"""Shared persistence for narration rows (`post_audio`).

Both the worker narrate stage and the web streaming endpoint upsert a
`post_audio` row keyed by (post_id, voice); this is the one place that DDL-aware
write lives so the two can't drift. Kept out of `worker.pipeline` so the web
process doesn't import the heavy pipeline/LLM/Playwright modules just to record
a narration.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PostAudio


async def upsert_post_audio(
    session: AsyncSession, post_id: int, voice: str, **fields
) -> None:
    """Insert-or-update the post_audio row for (post_id, voice). `onupdate=` is
    not applied by on_conflict_do_update, so updated_at is set explicitly."""
    stmt = pg_insert(PostAudio).values(post_id=post_id, voice=voice, **fields)
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_post_audio_post_voice",
            set_={**fields, "updated_at": func.now()},
        )
    )
