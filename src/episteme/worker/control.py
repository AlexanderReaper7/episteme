"""Pipeline pause/resume control.

The pause flag lives in `app_state` so it survives worker restarts and can be
flipped by anything that can reach the API — the usual caller is llama-warden,
which pauses LLM work while a game or other GPU-heavy application needs the card
(`worker/contention.py` is what its announcement does here). Pipeline loops call
`pause_requested()` between work units (one story / post / embed batch), so
pausing never abandons in-flight work: the current unit finishes, the run ends
cleanly with status "paused", and because stages are data-driven (they select
whatever rows are still unprocessed) the next deferred run resumes exactly where
this one stopped.

**A pause records who set it.** Two very different actors pause the pipeline: a
human, and the warden. Without a reason, a warden resuming when the GPU frees up
would also lift a pause the reader set by hand yesterday and expects to still be
holding. So the stored value carries `reason` — and `RESOURCE` is the only reason
the warden is allowed to clear (see `contention.apply_announcement`). A value
written before this field existed has no reason and reads as `MANUAL`, which is
the safe direction: automation leaves it alone.

Two timestamps are stored, and they answer different questions. `since` is when
the pause began — what the panel shows, and what a repeated announcement must
never move. `contended_at` is when we were last TOLD the GPU is still busy,
re-stamped by every re-announcement. It used to be when we last measured it
ourselves, and it anchored a resume window that this repository no longer
computes; the warden owns that timing now (0057). What it is good for here is
freshness: nothing expires, so a pause whose `contended_at` stopped advancing is
a warden that died rather than a GPU that is still busy. Both live here rather
than in a worker process that any redeploy would forget.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import AppState

log = logging.getLogger("episteme.control")

PAUSE_KEY = "pipeline_pause"
LEASE_KEY = "interactive_lease"

# Who may hold the interactive lease. Named constants rather than literals at the
# call sites so a hold and its release cannot drift apart by a typo — which would
# leak a holder until its TTL expired, with nothing to say why the GPU was busy.
CHAT_HOLDER = "chat"
BENCH_HOLDER = "benchmark"

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

    Called on every repeated `pause` announcement. The warden re-sends its
    verdict every 300s until it changes, so this is the difference between a
    panel that says "paused at 18:04" and one that says "paused just now" every
    time somebody looks — `since` is the fact, this is the heartbeat. A no-op
    when nothing is paused: there is nothing whose freshness to record."""
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


async def hold_interactive(
    session: AsyncSession, holder: str, seconds: float | None = None
) -> None:
    """Claim the models for work the reader is watching, under a named holder.

    A TTL, not a lock, and that asymmetry is the whole design. Chat runs in the
    **web** process, which `pipeline_job_running` cannot see: it counts
    procrastinate jobs, and a chat turn is not a job. A real lock held by a
    process that can be killed by `docker compose up -d web` would strand the
    GPU with nobody left to release it; an expiring lease heals itself in
    `chat_lease_seconds` no matter how the holder died. Refreshed every turn, so
    a long conversation stays covered without asking for a long lease.

    **The lease is a set of holders, not a single expiry.** There is more than one
    kind of deliberate GPU user now — a chat turn (minutes) and a benchmark run
    (hours) — and they overlap freely. With one shared expiry, whichever of them
    finished first handed the card back on the other's behalf: a chat turn ending
    mid-benchmark released the lease, and an automatic unload was then free to evict the
    model in the middle of a measurement.

    Written as one upsert rather than read-modify-write because the holders live
    in different processes (`web` and `worker`), so a Python-side merge under READ
    COMMITTED loses whichever update commits second — which is the same bug again,
    just rarer.
    """
    until = datetime.now(UTC) + timedelta(
        seconds=settings.chat_lease_seconds if seconds is None else seconds
    )
    # The entry is built in Python and cast, rather than assembled from a
    # `:holder`/`:until` pair in SQL, because `to_jsonb(:until::text)` does not
    # mean what it reads as: `text()` scans for `:name` with a negative lookahead
    # on `:`, so the postfix cast swallows the parameter and the statement binds
    # `unti`. `cast(… as jsonb)` says the same thing with no `::` in it.
    entry = json.dumps({holder: until.isoformat()})
    await session.execute(
        text(
            "INSERT INTO app_state (key, value, updated_at) "
            "VALUES (:key, jsonb_build_object('holders', cast(:entry as jsonb)), now()) "
            "ON CONFLICT (key) DO UPDATE SET value = jsonb_build_object("
            "  'holders', coalesce(app_state.value -> 'holders', '{}'::jsonb) "
            "             || cast(:entry as jsonb)), "
            "updated_at = now()"
        ),
        {"key": LEASE_KEY, "entry": entry},
    )
    await session.commit()


def _live(until: object) -> bool:
    """One holder's expiry, read defensively. Anything unparseable is treated as
    expired: a lease is a claim on 20GB of VRAM, and the safe reading of a value
    nobody can interpret is that nobody is holding it."""
    if not isinstance(until, str):
        return False
    try:
        expires = datetime.fromisoformat(until)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > datetime.now(UTC)


async def interactive_held(session: AsyncSession) -> bool:
    """Is anybody waiting on a model right now? Expired holders read as False, so
    nothing has to clean them up.

    A bare `until` is the pre-multi-holder shape and is still honoured, so an
    upgrade landing between two turns of a live conversation does not evict it.
    The next `hold_interactive` rewrites the value into the holders form."""
    value = (
        await session.execute(select(AppState.value).where(AppState.key == LEASE_KEY))
    ).scalar() or {}
    holders = value.get("holders") or {}
    return _live(value.get("until")) or any(_live(until) for until in holders.values())


async def release_interactive(session: AsyncSession, holder: str) -> None:
    """Give one holder's lease back early, when its work ends before the TTL does.

    Only that holder's. The other entries are somebody else's claim, and a
    benchmark measuring for an hour must survive a chat turn ending under it."""
    await session.execute(
        text(
            "UPDATE app_state SET value = jsonb_build_object("
            "  'holders', coalesce(value -> 'holders', '{}'::jsonb) - :holder), "
            "updated_at = now() WHERE key = :key"
        ),
        {"key": LEASE_KEY, "holder": holder},
    )
    await session.commit()


async def unload_unless_interactive(session: AsyncSession) -> list[str]:
    """Hand VRAM back, unless the reader is mid-conversation.

    Every automatic unload goes through here rather than calling
    `gateway.unload_models()` directly, so "do not evict a model somebody is
    watching stream" is one rule with one implementation. The gateway itself
    stays database-free (0003), which is why the check lives on this side of the
    call instead of inside it.

    `POST /api/llm/unload` deliberately does NOT route through here: an explicit
    manual unload is the reader overruling themselves, and it says so with a 409
    plus `?force=true` rather than silently doing nothing.
    """
    from ..llm import gateway

    if await interactive_held(session):
        log.info("Skipping unload: an interactive turn holds the models")
        return []
    return await gateway.unload_models()


async def pipeline_job_running(session: AsyncSession) -> bool:
    """Is a pipeline job in flight right now?

    Lives here because it is what makes a pause *safe* to act on: the pause flag
    alone says "stop at the next boundary", and everything that wants to do
    something destructive afterwards — unload the models, kill llama-server —
    has to know whether that boundary has been reached. One definition, shared
    by the API's graceful stop and the announcement applier, so the two cannot
    disagree about what "still working" means."""
    query = text(
        "SELECT count(*) FROM procrastinate_jobs WHERE status = 'doing' "
        "AND task_name IN ('episteme.run_pipeline', 'episteme.pipeline_stage')"
    )
    return bool((await session.execute(query)).scalar())
