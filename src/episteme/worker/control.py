"""Pipeline pause/resume control.

The pause flag lives in `app_state` so it survives worker restarts and can be
flipped by anything that can reach the API — the intended caller is a future
resource governor that pauses LLM work while a game or other GPU/CPU-heavy
application is running. Pipeline loops call `pause_requested()` between work
units (one story / post / embed batch), so pausing never abandons in-flight
work: the current unit finishes, the run ends cleanly with status "paused", and
because stages are data-driven (they select whatever rows are still
unprocessed) the next deferred run resumes exactly where this one stopped.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppState

PAUSE_KEY = "pipeline_pause"


async def pause_requested(session: AsyncSession) -> bool:
    """Fresh read every call (column select bypasses the identity-map cache;
    READ COMMITTED shows other transactions' commits between our own)."""
    value = (
        await session.execute(select(AppState.value).where(AppState.key == PAUSE_KEY))
    ).scalar()
    return bool(value and value.get("paused"))


async def set_paused(session: AsyncSession, paused: bool) -> None:
    state = await session.get(AppState, PAUSE_KEY)
    if state is None:
        state = AppState(key=PAUSE_KEY)
        session.add(state)
    state.value = {"paused": paused}
    await session.commit()
