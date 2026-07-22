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

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Voice as VoiceModel

DEFAULT_PROVIDER = "fish"


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


@dataclass(frozen=True, slots=True)
class ParamField:
    """One editable generation-param field in a provider's schema. `key` is the
    dotted path into the provider-agnostic `params` bag (e.g. `prosody.speed`),
    which is also the form input `name`, so parse/prefill stay generic."""

    key: str
    label: str
    kind: str = "number"  # number | text
    step: str | None = None
    min: float | None = None
    max: float | None = None
    placeholder: str = ""
    help: str = ""


# Per-provider params schema for the catalog editor. `params` is stored
# provider-agnostically (each provider reads the keys it understands); this
# describes those keys so the admin page can render + parse a provider's fields
# without the form hardcoding any single provider. A new provider adds an entry
# here (and the code that reads its keys) — no template or handler change.
PROVIDER_PARAM_SCHEMAS: dict[str, list[ParamField]] = {
    "fish": [
        ParamField("temperature", "temperature", step="0.05", min=0, placeholder="0.7",
                   help="Randomness of the synthesis (Fish default 0.7)."),
        ParamField("top_p", "top_p", step="0.05", min=0, max=1, placeholder="0.7",
                   help="Nucleus sampling cutoff."),
        ParamField("prosody.speed", "prosody.speed", step="0.05", min=0, placeholder="1.0",
                   help="Speaking rate; 1.0 is natural, <1 slower."),
        ParamField("prosody.volume", "prosody.volume", step="1", placeholder="0",
                   help="Loudness offset in dB; 0 leaves it unchanged."),
    ],
}


def param_schema_for(provider: str) -> list[ParamField]:
    """The editable param fields for a provider (empty for an unknown one)."""
    return PROVIDER_PARAM_SCHEMAS.get(provider, [])


def _set_dotted(target: dict, path: str, value: object) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        nxt = target.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            target[part] = nxt
        target = nxt
    target[parts[-1]] = value


def parse_params(provider: str, get_value: Callable[[str], str | None]) -> dict:
    """Build the params bag from a form-value getter, driven by the provider's
    schema. `get_value(key)` returns the raw submitted string for a (dotted) key;
    blanks are omitted and numeric fields coerced. Keys outside the schema are
    ignored, so a provider can never store params it doesn't understand."""
    params: dict = {}
    for spec in param_schema_for(provider):
        raw = (get_value(spec.key) or "").strip()
        if not raw:
            continue
        value: object = raw
        if spec.kind == "number":
            try:
                value = float(raw)
            except ValueError:
                continue
        _set_dotted(params, spec.key, value)
    return params


def flatten_params(params: dict | None) -> dict[str, object]:
    """Flatten a nested params bag to dotted keys mirroring `ParamField.key`
    (`{'prosody': {'speed': 1.0}}` → `{'prosody.speed': 1.0}`) so the editor can
    prefill each field by its dotted name."""
    out: dict[str, object] = {}

    def _walk(node: dict, prefix: str) -> None:
        for key, value in node.items():
            dotted = f"{prefix}{key}"
            if isinstance(value, dict):
                _walk(value, dotted + ".")
            else:
                out[dotted] = value

    _walk(params or {}, "")
    return out


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


async def reorder_voices(session: AsyncSession, ordered_ids: list[str]) -> None:
    """Set each voice's `sort_order` to its position in `ordered_ids` (index 0 →
    sort_order 0), so the first id becomes the lowest sort_order and therefore
    the effective default (see `pick_default`). Ids not present are left alone.
    Commits."""
    rows = {r.id: r for r in (await session.execute(select(VoiceModel))).scalars()}
    for index, voice_id in enumerate(ordered_ids):
        row = rows.get(voice_id)
        if row is not None:
            row.sort_order = index
    await session.commit()
