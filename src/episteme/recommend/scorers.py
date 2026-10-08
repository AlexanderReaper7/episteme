"""Ranking signals, as a plugin surface (spec §11: "one `Scorer` implementation
+ a weight in config").

Each scorer answers one narrow question about a candidate and returns a number
that is *positive when the reader should see more of this*. Sign lives in the
scorer, magnitude in config — so `scorer_weight_disliked` is a positive knob even
though the signal it controls subtracts.

Scores are combined linearly into `Post.affinity_score` and stored. The freshness
term is deliberately NOT here: it belongs to the feed query, so a post's stored
score never has to be rewritten just because time passed (see `web.app.feed_rank`).

Adding a signal: implement `score`, decorate with `@register`, add
`scorer_weight_<name>` to config. Nothing else changes — not the stage, not the
feed, not the schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from ..config import settings
from .profile import ProfileState

log = logging.getLogger("episteme.recommend.scorers")


@dataclass
class Candidate:
    """One post, flattened into everything the scorers need. Built once per post
    by the score stage so no scorer touches the database."""

    post_id: int
    kind: str = "article"
    # The story centroid — posts have no embedding of their own, and the centroid
    # is what the profile's liked/disliked centroids were themselves built from.
    embedding: list[float] | None = None
    # Canonical topic SLUGS, not the labels stored on the post: weights are keyed
    # by a topic's permanent identity, and `slugify(label)` stops finding it the
    # moment someone renames the topic. Build these with `topics.slug_for` against
    # one `topics.slug_index` per pass. Named for what it holds so a caller
    # handing over labels is a visible mistake, not a silently empty score.
    topic_slugs: list[str] = field(default_factory=list)
    source_ids: list[int] = field(default_factory=list)
    difficulty: str | None = None
    quality_score: float | None = None
    # Mean `credibility_rating` of the contributing sources (0..1, 0.5 = neutral).
    credibility: float = 0.5


class Scorer(Protocol):
    name: str

    def score(self, candidate: Candidate, profile: ProfileState) -> float: ...


_SCORERS: dict[str, Scorer] = {}


def register(scorer_cls: type) -> type:
    scorer = scorer_cls()
    _SCORERS[scorer.name] = scorer
    return scorer_cls


def registered() -> dict[str, Scorer]:
    return dict(_SCORERS)


def weight_for(name: str) -> float:
    """A registered scorer with no configured weight is inert, not fatal — that
    way a half-finished signal can sit in the tree without skewing the feed."""
    return float(getattr(settings, f"scorer_weight_{name}", 0.0))


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Both operands are normalized (gateway-normalized embeddings, normalized
    centroids), so the dot product IS the cosine; length mismatch scores 0 rather
    than raising, because a stale-dimension vector must not break ranking."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True))


@register
class LikedSimilarity:
    """Closeness to what the reader has liked — the signal that generalizes past
    the topic vocabulary, catching "this kind of thing" when no tag says so."""

    name = "liked"

    def score(self, candidate: Candidate, profile: ProfileState) -> float:
        return _cosine(candidate.embedding, profile.liked_centroid)


@register
class DislikedSimilarity:
    """Closeness to what the reader has rejected. Returns a NEGATIVE contribution
    so its config weight can stay a positive magnitude."""

    name = "disliked"

    def score(self, candidate: Candidate, profile: ProfileState) -> float:
        return -_cosine(candidate.embedding, profile.disliked_centroid)


@register
class TopicAffinity:
    """Learned weight of the post's topics. Averaged, not summed: a post tagged
    with four topics must not outscore a sharply-tagged one just for being
    broadly labelled."""

    name = "topic"

    def score(self, candidate: Candidate, profile: ProfileState) -> float:
        if not candidate.topic_slugs:
            return 0.0
        weights = [profile.topic_weights.get(s, 0.0) for s in candidate.topic_slugs]
        return sum(weights) / len(weights)


@register
class SourceAffinity:
    """Learned preference for the outlets behind the story (averaged, as above)."""

    name = "source"

    def score(self, candidate: Candidate, profile: ProfileState) -> float:
        if not candidate.source_ids:
            return 0.0
        weights = [profile.source_weights.get(str(s), 0.0) for s in candidate.source_ids]
        return sum(weights) / len(weights)


@register
class DifficultyMatch:
    """Whether the post is pitched where the reader asked to be pitched.
    `difficulty_weights` is a normalized distribution, so this is centred on an
    even split — matching the preferred level is a bonus, the others a penalty."""

    name = "difficulty"

    def score(self, candidate: Candidate, profile: ProfileState) -> float:
        if not candidate.difficulty or not profile.difficulty_weights:
            return 0.0
        even = 1.0 / len(profile.difficulty_weights)
        return profile.difficulty_weights.get(candidate.difficulty, 0.0) - even


@register
class Quality:
    """The pipeline's own verdict (triage rank, then QA's score), on a ~0-10 scale
    centred at 5 so a mediocre post is neutral rather than a positive. Not
    personalization — it is the floor that keeps a weakly-profiled feed sane."""

    name = "quality"

    def score(self, candidate: Candidate, profile: ProfileState) -> float:
        if candidate.quality_score is None:
            return 0.0
        return max(-1.0, min(1.0, (candidate.quality_score - 5.0) / 5.0))


@register
class Authority:
    """Source credibility as configured per source, centred on the 0.5 default so
    an unrated source contributes nothing either way."""

    name = "authority"

    def score(self, candidate: Candidate, profile: ProfileState) -> float:
        return (candidate.credibility - 0.5) * 2.0


def score_candidate(candidate: Candidate, profile: ProfileState) -> tuple[float, dict[str, float]]:
    """Combine every registered scorer. Returns the total and the per-signal
    breakdown, which is stored on the post: a ranking nobody can explain is a
    ranking nobody can debug (and is what the "why am I seeing this" chip will
    read)."""
    components: dict[str, float] = {}
    total = 0.0
    for name, scorer in _SCORERS.items():
        weight = weight_for(name)
        if not weight:
            continue
        raw = scorer.score(candidate, profile)
        if raw:
            contribution = weight * raw
            components[name] = round(contribution, 6)
            total += contribution
    return total, components
