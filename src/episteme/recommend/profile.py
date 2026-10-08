"""The interest profile — derived, never mutated.

The `feedback` table is canonical; `interest_profile` is a cache produced by
replaying it. Every read of the profile is therefore reproducible from the log,
which buys three things that in-place mutation cannot:

* **exact undo** — delete the event and rebuild; nothing residual survives;
* **retroactive retuning** — change the half-life or a signal's weight in config
  and the entire history is reinterpreted, instead of leaving a profile trained
  under the old constants;
* **no drift** — the profile can never disagree with what the reader actually did.

Replay is a single pass over a few hundred rows for one user, so "recompute
everything" is the cheap option as well as the correct one.

Decay is applied relative to *now* rather than incrementally between events,
which makes the whole thing a pure function of (events, now): a signal's
contribution is `weight * 0.5 ** (age_days / half_life_days)`. Interests fade
unless reinforced. It also means the profile drifts slowly on its own, so a
rebuild is worth running nightly even when no feedback arrived.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import EMBEDDING_DIM, Feedback, InterestProfile
from . import topics as topics_module

log = logging.getLogger("episteme.recommend.profile")

PROFILE_ID = 1

# Signals that speak about the *content* of one post: they move the centroids and
# spill weakly onto that post's topics and sources.
#
# `save` is deliberately NOT here (user decision, 2026-08-03): saving is
# bookmarking — "I want to find this again" — which is not the same claim as "show
# me more of this". A reader saves things to read later, to check a number in, to
# send to someone; treating that as approval trains the feed on an intention the
# reader never expressed. It is still recorded in the feedback log (it is a real
# thing the reader did, and the button renders from it), it just moves nothing.
_CONTENT_SIGNALS = {
    "like": lambda: settings.feedback_like_weight,
    "dislike": lambda: -settings.feedback_dislike_weight,
}


@dataclass
class ProfileState:
    """The replayed profile, in memory. Mirrors the InterestProfile columns; the
    scorer works against this, not the ORM row."""

    liked_centroid: list[float] | None = None
    disliked_centroid: list[float] | None = None
    topic_weights: dict[str, float] = field(default_factory=dict)
    source_weights: dict[str, float] = field(default_factory=dict)
    difficulty_weights: dict[str, float] = field(default_factory=dict)
    blocked_keywords: list[str] = field(default_factory=list)
    blocked_sources: list[int] = field(default_factory=list)
    intent_statement: str | None = None
    event_count: int = 0

    def topic_weight(self, key: str) -> float:
        """Weight for a topic, addressed by its slug — or by any spelling that
        slugifies to it, which is what makes "Marine Biology" from a card and
        "marine-biology" from a chip one weight rather than two.

        A stored LABEL is not necessarily one of those spellings: after a rename
        it slugifies to something the topic was never filed under. Go through
        `topics.slug_index` / `topics.slug_for` for anything read off a post."""
        return self.topic_weights.get(topics_module.slugify(key), 0.0)


def _decay(age_days: float) -> float:
    half_life = max(settings.feedback_half_life_days, 0.0001)
    return 0.5 ** (max(age_days, 0.0) / half_life)


def _normalize(vector: list[float]) -> list[float] | None:
    norm = sum(x * x for x in vector) ** 0.5
    if norm <= 1e-9:
        return None
    return [x / norm for x in vector]


def _clamp(value: float) -> float:
    limit = settings.profile_weight_clamp
    return max(-limit, min(limit, value))


def normalize_keyword(keyword: str | None) -> str:
    """The one definition of a blocked keyword's identity.

    Shared by every path that adds or removes one, because unblocking works by
    key: if a block written from a natural-language statement and an unblock
    written from the /tune chip normalized differently, the chip would silently
    fail to remove the block it is rendered from. `blocks.py` matches
    case-insensitively, so folding case here loses nothing."""
    return (keyword or "").strip().lower()


def replay(events: list[Feedback], now: datetime | None = None) -> ProfileState:
    """Fold the feedback log into a profile. Pure: same events + same `now` +
    same config always give the same result.

    Topic keys are the canonical SLUG the recorder resolved (`topics.resolve_slugs`),
    which is the topic's permanent identity — a later rename cannot re-key a
    weight learned months ago. The `slugify` calls below are a normalizer, not a
    derivation: a slug slugifies to itself, so they cost nothing for a
    well-formed row and they keep rows written before slugs were stored (which
    hold labels) landing on exactly the key they always did.
    """
    now = now or datetime.now(UTC)
    state = ProfileState(event_count=len(events))
    liked_sum = [0.0] * EMBEDDING_DIM
    disliked_sum = [0.0] * EMBEDDING_DIM
    topic_raw: dict[str, float] = {}
    source_raw: dict[str, float] = {}
    difficulty_raw: dict[str, float] = {}
    blocked_keywords: dict[str, None] = {}  # ordered set
    blocked_sources: dict[int, None] = {}

    def bump(bucket: dict, key, amount: float) -> None:
        bucket[key] = bucket.get(key, 0.0) + amount

    for event in sorted(events, key=lambda e: (e.created_at, e.id)):
        created = event.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        decay = _decay((now - created).total_seconds() / 86400.0)
        if decay <= 0.0:
            continue

        if event.kind in _CONTENT_SIGNALS:
            strength = _CONTENT_SIGNALS[event.kind]() * decay
            vector = list(event.embedding) if event.embedding is not None else None
            if vector is not None and len(vector) == EMBEDDING_DIM:
                target = liked_sum if strength > 0 else disliked_sum
                for index, value in enumerate(vector):
                    target[index] += abs(strength) * value
            spill = strength * settings.feedback_topic_spillover
            for label in event.topics_snapshot or []:
                bump(topic_raw, topics_module.slugify(label), spill)
            source_spill = settings.feedback_source_step * (1 if strength > 0 else -1) * decay
            for source_id in event.source_ids or []:
                bump(source_raw, str(source_id), source_spill)
            if event.difficulty:
                bump(difficulty_raw, event.difficulty, strength)

        elif event.kind in ("more_topic", "less_topic"):
            if event.topic:
                sign = 1 if event.kind == "more_topic" else -1
                bump(
                    topic_raw,
                    topics_module.slugify(event.topic),
                    sign * settings.feedback_topic_step * decay,
                )

        elif event.kind == "set_topic":
            # A hand-edited weight is absolute: it REPLACES everything the log had
            # accumulated for that topic up to this point, rather than nudging it.
            # Two consequences, both deliberate:
            #
            # * it is not decayed. The reader typed a number and the panel shows
            #   that number; a control whose value drifts on its own is a control
            #   that lies. Like `hide_source`, this is a stated position rather
            #   than a reaction to one post.
            # * it is an anchor, not a lock. Later likes and steering still move
            #   the weight from here — the feed keeps learning, it just starts
            #   from where it was told to.
            #
            # Setting 0 is therefore a real operation: it discards the history and
            # drops the topic below the noise floor, which is what "forget this"
            # has to mean. The event itself stays in the log, so undoing it
            # restores exactly the weight that was there before.
            if event.topic and event.value is not None:
                topic_raw[topics_module.slugify(event.topic)] = _clamp(float(event.value))

        elif event.kind == "hide_source":
            # A hard block, not a weight: it is not subject to decay, because the
            # reader said "not this outlet", not "less of this outlet lately".
            for source_id in event.source_ids or []:
                blocked_sources[source_id] = None

        elif event.kind in ("block_keyword", "unblock_keyword"):
            # Blocks are a set the log folds over in time order, so the reader's
            # most recent word wins: unblocking beats an earlier block (from a
            # button OR from a natural-language refusal), and a later statement
            # that refuses the word again re-blocks it. That is why unblocking is
            # its own event rather than the deletion of whatever imposed the block
            # — the nl statement that also set topic weights stays intact.
            cleaned = normalize_keyword(event.keyword)
            if cleaned:
                if event.kind == "block_keyword":
                    blocked_keywords[cleaned] = None
                else:
                    blocked_keywords.pop(cleaned, None)

        elif event.kind == "nl_feedback":
            intent = event.parsed_intent or {}
            for entry in intent.get("topics") or []:
                label = entry.get("topic")
                if not label:
                    continue
                sign = 1 if entry.get("direction") == "more" else -1
                strength = float(entry.get("strength") or 1.0)
                bump(
                    topic_raw,
                    topics_module.slugify(label),
                    sign * strength * settings.feedback_topic_step * decay,
                )
            for keyword in intent.get("blocked_keywords") or []:
                cleaned = normalize_keyword(keyword)
                if cleaned:
                    blocked_keywords[cleaned] = None
            if intent.get("difficulty"):
                bump(difficulty_raw, intent["difficulty"], decay)
            if event.nl_text:
                # The most recent statement wins as the displayed one.
                state.intent_statement = event.nl_text

    state.liked_centroid = _normalize(liked_sum)
    state.disliked_centroid = _normalize(disliked_sum)
    state.topic_weights = {
        slug: _clamp(weight) for slug, weight in sorted(topic_raw.items()) if abs(weight) > 1e-6
    }
    state.source_weights = {
        key: _clamp(weight) for key, weight in sorted(source_raw.items()) if abs(weight) > 1e-6
    }
    total_difficulty = sum(abs(v) for v in difficulty_raw.values())
    state.difficulty_weights = (
        {key: value / total_difficulty for key, value in sorted(difficulty_raw.items())}
        if total_difficulty > 1e-6
        else {}
    )
    state.blocked_keywords = list(blocked_keywords)
    state.blocked_sources = list(blocked_sources)
    return state


def describe(state: ProfileState, limit: int = 8, labels: dict[str, str] | None = None) -> str:
    """The profile as a short paragraph for a prompt.

    Deliberately qualitative — "strongly interested in", not "weight 4.2". The
    numbers are an artifact of the learning rules, and handing a model a scale it
    has no calibration for invites it to do arithmetic on the reader's tastes
    instead of using them as context. Returns "" for a cold profile, so callers
    add nothing to the prompt rather than describing an absence of preferences.

    `labels` maps slug -> the vocabulary's current spelling; without it a slug
    reads as its own words, which is the right fallback but goes stale after a
    rename.
    """
    if not state.topic_weights and not state.difficulty_weights and not state.intent_statement:
        return ""

    def name(slug: str) -> str:
        return (labels or {}).get(slug) or slug.replace("-", " ")

    ranked = sorted(state.topic_weights.items(), key=lambda kv: -abs(kv[1]))[:limit]
    likes = [name(slug) for slug, weight in ranked if weight > 0.5]
    dislikes = [name(slug) for slug, weight in ranked if weight < -0.5]

    lines = []
    if likes:
        lines.append(f"Interested in: {', '.join(likes)}.")
    if dislikes:
        lines.append(f"Wants less of: {', '.join(dislikes)}.")
    # Only a POSITIVE share is a preference. The distribution is normalized by the
    # sum of absolute values, so it can be entirely negative — one dislike of a
    # technical post and nothing else leaves {"technical": -1.0}, and taking the
    # max of that would tell triage and the writer "Prefers technical depth" on
    # the strength of the reader rejecting exactly that.
    preferred = max(state.difficulty_weights.items(), key=lambda kv: kv[1], default=(None, 0.0))
    if preferred[1] > 0:
        lines.append(f"Prefers {preferred[0]} depth.")
    elif state.difficulty_weights:
        avoided = [level for level, share in state.difficulty_weights.items() if share < 0]
        if avoided:
            lines.append(f"Not looking for {', '.join(sorted(avoided))} depth.")
    if state.intent_statement:
        lines.append(f'In their own words: "{state.intent_statement.strip()}"')
    return " ".join(lines)


# --- Persistence ------------------------------------------------------------------


async def rebuild(session: AsyncSession, now: datetime | None = None) -> ProfileState:
    """Replay the whole feedback log and persist the result. This is the ONLY way
    the profile row is written — there is no incremental update path, by design."""
    events = list((await session.execute(select(Feedback))).scalars().all())
    state = replay(events, now=now)
    row = await session.get(InterestProfile, PROFILE_ID)
    if row is None:
        row = InterestProfile(id=PROFILE_ID)
        session.add(row)
    row.liked_centroid = state.liked_centroid
    row.disliked_centroid = state.disliked_centroid
    row.topic_weights = state.topic_weights
    row.source_weights = state.source_weights
    row.difficulty_weights = state.difficulty_weights
    row.blocked_keywords = state.blocked_keywords
    row.blocked_sources = state.blocked_sources
    row.intent_statement = state.intent_statement
    row.event_count = state.event_count
    row.rebuilt_at = now or datetime.now(UTC)
    await session.commit()
    log.info(
        "Profile rebuilt from %d events: %d topic weights, %d blocked sources",
        state.event_count,
        len(state.topic_weights),
        len(state.blocked_sources),
    )
    return state


async def load(session: AsyncSession) -> ProfileState:
    """The cached profile as a ProfileState. An absent row is an empty profile —
    a cold start scores as pure freshness, never as an error."""
    row = await session.get(InterestProfile, PROFILE_ID)
    if row is None:
        return ProfileState()
    return ProfileState(
        liked_centroid=list(row.liked_centroid) if row.liked_centroid is not None else None,
        disliked_centroid=(
            list(row.disliked_centroid) if row.disliked_centroid is not None else None
        ),
        topic_weights=dict(row.topic_weights or {}),
        source_weights=dict(row.source_weights or {}),
        difficulty_weights=dict(row.difficulty_weights or {}),
        blocked_keywords=list(row.blocked_keywords or []),
        blocked_sources=list(row.blocked_sources or []),
        intent_statement=row.intent_statement,
        event_count=row.event_count,
    )
