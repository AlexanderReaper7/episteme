"""The narration voice catalog — now a database table (`voices`).

Voices live in the DB (seeded by `seeds.seed_voices`) so they and their
generation parameters are managed as data, and a future provider can carry its
own params without a code change. This module is the typed read layer over that
table: a plain `Voice` dataclass plus async accessors. The pure `pick_default`
helper (default-voice resolution) is factored out so it can be unit-tested
without a database — the DB accessors themselves are thin queries, tested live.

The catalog key (`Voice.id`) is, for the Fish provider, the Fish `reference_id`,
so it is also the value in `post_audio.voice`, the on-disk filename, and the
`?voice=` query param. `provider_voice_id` is what the provider is addressed
with (defaults to `id`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Voice as VoiceModel


@dataclass(frozen=True, slots=True)
class Voice:
    id: str
    label: str
    provider: str = "fish"
    provider_voice_id: str | None = None
    params: dict = field(default_factory=dict)
    sort_order: int = 0
    enabled: bool = True

    @property
    def ref_id(self) -> str:
        """The id the provider is addressed with (the Fish reference_id)."""
        return self.provider_voice_id or self.id


def _to_voice(row: VoiceModel) -> Voice:
    return Voice(
        id=row.id,
        label=row.label,
        provider=row.provider,
        provider_voice_id=row.provider_voice_id,
        params=dict(row.params or {}),
        sort_order=row.sort_order,
        enabled=row.enabled,
    )


def pick_default(voices: list[Voice], configured: str | None) -> str | None:
    """The effective default voice id: the configured override if it names a
    voice in the (enabled) list, else the lowest sort_order voice, else None for
    an empty catalog. Pure so it's unit-testable without a database."""
    by_id = {v.id: v for v in voices}
    if configured and configured in by_id:
        return configured
    if not voices:
        return None
    return min(voices, key=lambda v: (v.sort_order, v.id)).id


async def list_voices(session: AsyncSession, enabled_only: bool = True) -> list[Voice]:
    query = select(VoiceModel).order_by(VoiceModel.sort_order, VoiceModel.id)
    if enabled_only:
        query = query.where(VoiceModel.enabled.is_(True))
    rows = (await session.execute(query)).scalars().all()
    return [_to_voice(r) for r in rows]


async def get_voice(session: AsyncSession, voice_id: str | None) -> Voice | None:
    """The voice by catalog id (enabled or not — a stored `post_audio.voice`
    should still resolve for playback). None if it doesn't exist."""
    if not voice_id:
        return None
    row = await session.get(VoiceModel, voice_id)
    return _to_voice(row) if row else None


async def default_voice_id(session: AsyncSession, configured: str | None) -> str | None:
    """The effective default among enabled voices (see `pick_default`)."""
    return pick_default(await list_voices(session, enabled_only=True), configured)


async def upsert_voice(
    session: AsyncSession,
    *,
    id: str,
    label: str,
    provider: str = "fish",
    provider_voice_id: str | None = None,
    params: dict | None = None,
    sort_order: int = 0,
    enabled: bool = True,
) -> Voice:
    """Create a voice, or overwrite the row with this `id` if it already exists
    (the catalog key is user-supplied — for Fish, the reference_id). Commits."""
    row = await session.get(VoiceModel, id)
    if row is None:
        row = VoiceModel(id=id)
        session.add(row)
    row.label = label
    row.provider = provider
    row.provider_voice_id = provider_voice_id or None
    row.params = params or {}
    row.sort_order = sort_order
    row.enabled = enabled
    await session.commit()
    return _to_voice(row)


async def set_voice_enabled(session: AsyncSession, voice_id: str, enabled: bool) -> None:
    row = await session.get(VoiceModel, voice_id)
    if row is not None:
        row.enabled = enabled
        await session.commit()


async def delete_voice(session: AsyncSession, voice_id: str) -> None:
    row = await session.get(VoiceModel, voice_id)
    if row is not None:
        await session.delete(row)
        await session.commit()
