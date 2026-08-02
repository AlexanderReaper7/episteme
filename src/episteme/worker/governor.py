"""Resource governor — yields the GPU to whatever else is using it.

Architecture §7 ("Scheduling & idle behavior") specified a host-side agent that
watches the machine and gates LLM processing. This is the Episteme half of it:
`hostagent/llama_agent.py` measures, this decides, and the decision is expressed
through the pause flag that already exists (`worker/control.py`) rather than
through a second, parallel notion of "allowed to run".

**The rule, as the user set it:** other work takes priority, but only where
Episteme would *noticeably* degrade it. So this governs on resource contention,
NOT on whether someone is at the keyboard — a reader typing an email is not a
reason to stop writing articles, and a game left running while they are away
still is.

Direction of control is a pull, not a push (the spec sketched the agent flipping
a flag via the API). Three reasons: the agent stays stateless and needs no
credentials for Episteme; every threshold lives in Episteme's config beside the
rest of the tuning; and an agent that dies leaves *no opinion* — which degrades
to exactly today's behaviour — instead of a stale flag with nobody left to clear
it.

Both signals are used only where they are valid, which is what the measurements
on the target box actually support:

* `foreign_gpu_percent` is per-process utilization minus our own llama-server
  processes. Attribution makes it truthful whether or not we are generating, so
  it works as a *pause* signal and not merely as a start gate.
* Free VRAM cannot be attributed at all (the Windows per-process memory counter
  reported 22 GB for dwm on a 10 GB card — it counts committed, not resident).
  It is therefore consulted only while our own models are unloaded, where the
  whole figure is by definition someone else's. While we hold models it is
  ignored rather than guessed at.
"""

import logging
from datetime import UTC, datetime

from ..config import settings
from ..db import SessionLocal
from ..llm import gateway
from ..llm.host import host_agent
from .app import app
from .control import RESOURCE, mark_contended, pause_state, pipeline_job_running, set_paused

log = logging.getLogger("episteme.governor")


def is_contended(
    resources: dict,
    *,
    our_models_loaded: bool,
    busy_percent: float,
    min_free_vram_mb: int,
) -> tuple[bool, str]:
    """Is someone else's work on this GPU at stake? Returns (contended, why).

    `why` is carried through to the log and the admin panel: a pipeline that
    stopped on its own must be able to say what it saw, or the feature is
    indistinguishable from a bug."""
    foreign = resources.get("foreign_gpu_percent")
    if foreign is not None and foreign >= busy_percent:
        games = resources.get("games_running") or []
        detail = f" ({', '.join(games)})" if games else ""
        return True, f"foreign GPU load {foreign:.0f}% >= {busy_percent:.0f}%{detail}"

    free = resources.get("vram_free_mb")
    if not our_models_loaded and free is not None and free < min_free_vram_mb:
        return True, f"only {free} MB VRAM free, need {min_free_vram_mb} MB to load a model"

    return False, "GPU is free"


def decide(
    resources: dict,
    state: dict,
    *,
    now: datetime,
    our_models_loaded: bool,
    busy_percent: float,
    min_free_vram_mb: int,
    resume_quiet_seconds: int,
) -> tuple[str | None, str]:
    """Pure policy: returns (action, reason) where action is "pause", "hold",
    "resume" or None. Pure so the whole decision table is testable without a GPU,
    a host agent, or a database — the same reason `sweep_stalled_jobs` takes its
    manager as an argument.

    Asymmetric by design: yield the moment contention appears, return only after
    the GPU has been quiet for a while. Restarting a 20 GB model load during a
    lull between two loading screens is worse than waiting — which is what
    "hold" is for: contention seen while already paused re-stamps
    `contended_at`, so the resume window measures the *quiet*, not the pause. An
    unrefreshed window would elapse under a running game, and the first
    momentary dip after that would resume into it."""
    contended, why = is_contended(
        resources,
        our_models_loaded=our_models_loaded,
        busy_percent=busy_percent,
        min_free_vram_mb=min_free_vram_mb,
    )

    if contended:
        return ("hold", f"already paused: {why}") if state["paused"] else ("pause", why)

    if not state["paused"]:
        return None, why
    # Never lift a pause this governor did not set. A human who paused by hand
    # expects it to hold until they say otherwise.
    if state["reason"] != RESOURCE:
        return None, f"paused by {state['reason']}; not ours to resume"

    quiet_for = _seconds_since(state.get("contended_at"), now)
    if quiet_for is not None and quiet_for < resume_quiet_seconds:
        return None, f"{why}, but only for {quiet_for:.0f}s of {resume_quiet_seconds}s"
    return "resume", why


def _seconds_since(timestamp: str | None, now: datetime) -> float | None:
    """None when the flag predates the timestamp — treated as "long enough",
    since the alternative is a pause that can never lift."""
    if not timestamp:
        return None
    try:
        started = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (now - started).total_seconds()


async def _our_models_loaded() -> bool:
    """Whether any of our endpoints is holding a model in VRAM. Delegated to the
    gateway so the "is this row occupying VRAM?" test is shared with
    `unload_models` — see `gateway.holds_vram` for why a disagreement between
    those two was a real defect and not a stylistic one."""
    try:
        return await gateway.models_loaded()
    except Exception:  # a down endpoint holds nothing
        return False


@app.task(name="episteme.govern_resources")
async def govern_resources() -> dict:
    """One decision. Independently deferrable so the policy can be exercised on
    demand instead of waiting for the cron."""
    if not (settings.resource_governor_enabled and host_agent.enabled):
        return {"action": None, "reason": "governor disabled"}

    resources = await host_agent.resources()
    if resources is None:
        # No opinion, deliberately: see the module docstring. An agent that died
        # must not be able to strand the pipeline in either state.
        return {"action": None, "reason": "host agent unavailable"}

    unload = False
    async with SessionLocal() as session:
        state = await pause_state(session)
        action, reason = decide(
            resources,
            state,
            now=datetime.now(UTC),
            our_models_loaded=await _our_models_loaded(),
            busy_percent=settings.resource_gpu_busy_percent,
            min_free_vram_mb=settings.resource_min_free_vram_mb,
            resume_quiet_seconds=settings.resource_resume_quiet_seconds,
        )
        if action == "pause":
            await set_paused(session, True, reason=RESOURCE)
            # The worker finishes its current unit before it stops, so whether
            # we may take the VRAM away right now is exactly "is one running?".
            # Same guard as /api/pipeline/pause, same reason: unloading under a
            # generation in flight destroys the story we were trying to preserve
            # by pausing gently in the first place.
            unload = not await pipeline_job_running(session)
        elif action == "hold":
            await mark_contended(session)
        elif action == "resume":
            await set_paused(session, False)

    if action == "pause":
        log.info("Resource governor paused the pipeline: %s", reason)
        if unload:
            # Nothing was running, so the VRAM a sleeping router still holds is
            # handed back now rather than at 03:00. When something IS running it
            # unloads at its own next unit boundary (pipeline.py).
            await gateway.unload_models()
    elif action == "resume":
        log.info("Resource governor resumed the pipeline: %s", reason)
        # Imported here, not at module scope: pipeline imports the world, and
        # the governor is meant to stay cheap enough to run every two minutes.
        from .pipeline import run_pipeline

        await run_pipeline.defer_async()

    return {"action": action, "reason": reason, "resources": resources}


@app.task(name="episteme.scheduled_govern_resources")
async def scheduled_govern_resources(timestamp: int) -> None:
    await govern_resources.defer_async()


def periodic_enabled() -> bool:
    """Whether the cron below is worth registering — the same two conditions
    `govern_resources` itself checks. A named predicate rather than an inline
    `if`, because a module-level side effect is otherwise untestable: the module
    can only be imported once per process, so the branch that was not taken can
    never be observed."""
    return bool(settings.resource_governor_enabled and settings.llm_host_agent_url)


# Registered only when the feature is actually on. A disabled governor returns
# immediately, but a `*/2` periodic still writes ~720 rows a day into
# `procrastinate_jobs` to do it — which nothing prunes, and which drowns the
# 20-row admin queue view that exists to show what the pipeline is doing. Off
# means off, not "cheap".
if periodic_enabled():
    app.periodic(cron=settings.resource_governor_cron)(scheduled_govern_resources)
