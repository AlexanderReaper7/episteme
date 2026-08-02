"""Pipeline pause/resume control.

The pause flag lives in `app_state` so it survives worker restarts and can be
flipped by anything that can reach the API — the intended caller is the resource
governor (`worker/governor.py`), which pauses LLM work while a game or other
GPU-heavy application needs the card. Pipeline loops call `pause_requested()`
between work units (one story / post / embed batch), so pausing never abandons
in-flight work: the current unit finishes, the run ends cleanly with status
"paused", and because stages are data-driven (they select whatever rows are
still unprocessed) the next deferred run resumes exactly where this one stopped.

**A pause records who set it.** Two very different actors pause the pipeline: a
human, and an automatic governor. Without a reason, a governor that resumes when
the GPU frees up would also lift a pause the reader set by hand yesterday and
expects to still be holding. So the stored value carries `reason` — and
`RESOURCE` is the only reason the governor is allowed to clear (see
`governor.decide`). A value written before this field existed has no reason and
reads as `MANUAL`, which is the safe direction: automation leaves it alone.

Two timestamps are stored, and they answer different questions. `since` is when
the pause began — what the panel shows. `contended_at` is when contention was
last *observed*, refreshed on every governor tick that still sees a busy GPU,
and it is what the resume window is measured against: a window anchored to the
pause start would expire while the game was still running, so the first
momentary dip after it elapsed would resume — the exact "lull between two
loading screens" the design exists to prevent. Both live here rather than in a
worker process that any redeploy would forget.
"""

from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppState

PAUSE_KEY = "pipeline_pause"

MANUAL = "manual"
RESOURCE = "resource"


async def pause_requested(session: AsyncSession) -> bool:
    """Fresh read every call (column select bypasses the identity-map cache;
    READ COMMITTED shows other transactions' commits between our own)."""
    return (await pause_state(session))["paused"]


async def pause_state(session: AsyncSession) -> dict:
    """The whole flag: `{"paused", "reason", "since", "contended_at"}`.

    `reason` is None only when not paused; a legacy row without one reads as
    MANUAL so automation cannot clear it. `contended_at` falls back to `since`
    for rows written before it existed — the same "assume the older, safer
    reading" direction."""
    value = (
        await session.execute(select(AppState.value).where(AppState.key == PAUSE_KEY))
    ).scalar() or {}
    paused = bool(value.get("paused"))
    since = value.get("since") if paused else None
    return {
        "paused": paused,
        "reason": (value.get("reason") or MANUAL) if paused else None,
        "since": since,
        "contended_at": (value.get("contended_at") or since) if paused else None,
    }


async def set_paused(session: AsyncSession, paused: bool, reason: str = MANUAL) -> None:
    """Set or clear the pause. `reason` is recorded only when pausing — a cleared
    flag has no author to remember."""
    now = datetime.now(UTC).isoformat()
    await _write(
        session,
        {"paused": True, "reason": reason, "since": now, "contended_at": now}
        if paused
        else {"paused": False},
    )


async def mark_contended(session: AsyncSession) -> None:
    """Re-stamp `contended_at` on an existing pause without moving `since`.

    Called by the governor on every tick that still sees a busy GPU, so the
    resume window measures how long the GPU has been *quiet* rather than how
    long we have been paused. A no-op when nothing is paused: there is no window
    to hold open."""
    state = await session.get(AppState, PAUSE_KEY)
    if state is None or not (state.value or {}).get("paused"):
        return
    state.value = {**state.value, "contended_at": datetime.now(UTC).isoformat()}
    await session.commit()


async def restore_pause(session: AsyncSession, snapshot: dict) -> None:
    """Write a `pause_state` snapshot back verbatim.

    For rollback: a caller that pauses, then fails at the thing it paused *for*,
    must be able to leave the flag exactly as it found it — including whose
    pause it was and when it started. Re-deriving that with `set_paused` would
    reset `since` and silently re-author the pause."""
    await _write(
        session,
        {
            "paused": True,
            "reason": snapshot["reason"],
            "since": snapshot["since"],
            "contended_at": snapshot["contended_at"],
        }
        if snapshot["paused"]
        else {"paused": False},
    )


async def _write(session: AsyncSession, value: dict) -> None:
    state = await session.get(AppState, PAUSE_KEY)
    if state is None:
        state = AppState(key=PAUSE_KEY)
        session.add(state)
    state.value = value
    await session.commit()


async def pipeline_job_running(session: AsyncSession) -> bool:
    """Is a pipeline job in flight right now?

    Lives here because it is what makes a pause *safe* to act on: the pause flag
    alone says "stop at the next boundary", and everything that wants to do
    something destructive afterwards — unload the models, kill llama-server —
    has to know whether that boundary has been reached. One definition, shared
    by the API's graceful stop and the governor, so the two cannot disagree
    about what "still working" means."""
    query = text(
        "SELECT count(*) FROM procrastinate_jobs WHERE status = 'doing' "
        "AND task_name IN ('episteme.run_pipeline', 'episteme.pipeline_stage')"
    )
    return bool((await session.execute(query)).scalar())
