"""What llama-warden's announcement means here.

Episteme used to decide this itself: a `*/2` cron asked the host agent what the
GPU looked like, applied a threshold from `settings`, and paused. That policy
left in 2026-09 (0057). The measuring process is now the deciding process, and
this module is the receiving end of its verdict — `POST /api/pipeline/announce`
is the door, this is what is behind it.

**The warden decides *that* we should stop. This decides what stopping means**,
which is the seam the split exists to create: pausing the pipeline, and handing
VRAM back if nothing is mid-story. A second consumer of the same GPU would do
something else entirely with the same message.

Three rules survive from the governor, unchanged, because they were never about
who owned the threshold:

* **Only a RESOURCE pause may be lifted.** A human who paused by hand expects it
  to hold until they say otherwise, and the warden has no way to know one from
  the other. `control.pause_state` reads a pause with no author as MANUAL, which
  is the safe direction.
* **A pause does not take VRAM out from under a running story.** The worker
  stops at its next unit boundary, so "may we unload right now?" is exactly "is
  one running?" — and if one is, it unloads itself when it gets there.
* **An interactive turn outranks the unload.** `unload_unless_interactive`, not
  `gateway.unload_models`. The warden may well be right that a game needs the
  card; pulling a model out from under a stream somebody is watching is still a
  different act.

What is new is that the announcement is **repeated**, every 300 s, until the
warden's verdict changes — the warden pushes rather than leasing, so repetition
is its only retry. Applying a `pause` to an already-paused pipeline must
therefore not move `since`, or the panel's "paused at 18:04" becomes "paused at
whenever you last looked". It re-stamps `contended_at` instead, which is now
**when the warden last said so** rather than when we last measured. That is the
one fact that makes a stranded pause visible: nothing expires, so a pause whose
`contended_at` stopped advancing is a warden that died (llama-warden 0001).
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from .control import (
    RESOURCE,
    mark_contended,
    pause_state,
    pipeline_job_running,
    set_paused,
    unload_unless_interactive,
)

log = logging.getLogger("episteme.contention")

PAUSE = "pause"
RESUME = "resume"


async def apply_announcement(session: AsyncSession, action: str, reason: str) -> dict:
    """Apply one verdict. Returns what was done and why, which is the body the
    endpoint hands back — a warden that is told "applied: false, someone paused
    this by hand" can say so in its own log instead of retrying into a wall.

    Idempotent by construction: every branch below is a function of the stored
    state and the incoming action, never of whether this is the first time we
    have been told."""
    state = await pause_state(session)

    if action == PAUSE:
        return await _pause(session, state, reason)
    if action == RESUME:
        return await _resume(session, state, reason)
    raise ValueError(f"Unknown action {action!r}; expected {PAUSE!r} or {RESUME!r}")


async def _pause(session: AsyncSession, state: dict, reason: str) -> dict:
    if state["paused"]:
        # Already stopped. Re-stamp the freshness clock and touch nothing else:
        # not `since`, and not `reason`, because promoting a hand-set pause to
        # RESOURCE would hand the warden permission to lift it later.
        await mark_contended(session)
        return {
            "applied": False,
            "paused": True,
            "detail": f"already paused by {state['reason']}",
        }

    await set_paused(session, True, reason=RESOURCE)
    # Read after the write, in the same session: the worker finishes its current
    # unit before it stops, so this is the difference between handing VRAM back
    # now and handing it back at the next boundary.
    running = await pipeline_job_running(session)
    unloaded: list[str] = []
    if not running:
        unloaded = await unload_unless_interactive(session)
    log.info("llama-warden paused the pipeline: %s", reason)
    return {
        "applied": True,
        "paused": True,
        "worker_running": running,
        "unloaded_models": unloaded,
    }


async def _resume(session: AsyncSession, state: dict, reason: str) -> dict:
    if not state["paused"]:
        return {"applied": False, "paused": False, "detail": "not paused"}
    if state["reason"] != RESOURCE:
        return {
            "applied": False,
            "paused": True,
            "detail": f"paused by {state['reason']}; not the warden's to lift",
        }

    await set_paused(session, False)
    log.info("llama-warden resumed the pipeline: %s", reason)
    # Deferred BY NAME, the same way `/api/pipeline/resume` does it. This runs in
    # the web process, so importing `pipeline` to reach the task object would
    # pull the whole stage graph into it for the sake of one string.
    from .app import app as job_app

    async with job_app.open_async():
        job_id = await job_app.configure_task("episteme.run_pipeline").defer_async()
    return {"applied": True, "paused": False, "job_id": job_id}
