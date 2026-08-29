from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates

# Dimension of the `embed` model role output. 1024 = native dim of
# Octen-Embedding-0.6B; larger models (4B = 2560) are truncated + re-normalized
# by the gateway (Matryoshka). Changing this requires an Alembic revision that
# rewrites both vector columns, and re-embedding everything — old-dimension
# vectors cannot be cast, so the migration has to null them deliberately.
EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    type_name: Mapped[str] = mapped_column(String(50))  # adapter registry key, e.g. "rss"
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # adapter-specific
    enabled: Mapped[bool] = mapped_column(default=True)
    credibility_rating: Mapped[float] = mapped_column(default=0.5)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # HTTP cache validators from the last feed response (conditional GET support).
    http_etag: Mapped[str | None] = mapped_column(Text)
    http_last_modified: Mapped[str | None] = mapped_column(Text)
    # Set when the source rate-limits us; ingestion skips it until this passes.
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    items: Mapped[list[SourceItem]] = relationship(back_populates="source")


class SourceItem(Base):
    __tablename__ = "source_items"
    __table_args__ = (Index("ix_source_items_published_at", "published_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    story_id: Mapped[int | None] = mapped_column(ForeignKey("stories.id"))
    url: Mapped[str] = mapped_column(Text)
    # sha256 of whatever identifies this item to its producer. Ingestion hashes
    # the canonical URL, and article dedup depends on that: two feed entries for
    # one URL have to collide. A correspondent hashes its period key instead
    # (0046), e.g. `Matsedel/koppargrillen/2026w35`, because it re-reads one
    # unchanging URL every week and the column is unique. Still a 64-char digest;
    # only the choice of what goes into it belongs to the producer.
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_content: Mapped[str | None] = mapped_column(Text)  # feed-provided summary/body
    extracted_text: Mapped[str | None] = mapped_column(Text)  # full-article extraction
    media_refs: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(255))
    arxiv_id: Mapped[str | None] = mapped_column(String(50))
    fetch_status: Mapped[str] = mapped_column(String(30), default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    source: Mapped[Source] = relationship(back_populates="items")
    story: Mapped[Story | None] = relationship(back_populates="items")


# The primary item of a story: earliest publish date, then lowest id (0047). It
# supplies an aggregate card's title, source name, snippet AND destination, which
# is why it is one definition rather than four call sites. `nulls_last` is what
# Postgres already does for ASC, written out so the ordering stays total if an
# item ever arrives without a publish date.
PRIMARY_ITEM_ORDER = (SourceItem.published_at.asc().nulls_last(), SourceItem.id.asc())


def primary_item_key(item: SourceItem) -> tuple[datetime, int]:
    """`PRIMARY_ITEM_ORDER` evaluated in Python, for the one caller that compares
    an item still being clustered against a story's current primary. The two have
    to agree, so they are written next to each other."""
    return (item.published_at or datetime.max.replace(tzinfo=UTC), item.id)


class Story(Base):
    """A cluster of SourceItems about the same underlying event/paper (spec §4)."""

    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Who asked for this story. "ingest" is the pipeline's own clustering; "user"
    # is an explicit request through the assistant, which outranks the pipeline:
    # the writer is offered no `demote_story`, the thin-gate does not apply, and
    # it sorts to the front of the write queue. A property of the story rather
    # than of the job, so it survives a retry and a later rewrite.
    origin: Mapped[str] = mapped_column(String(20), default="ingest")
    # new -> triaged (decision recorded) -> written | aggregated | skipped.
    # `filed` is a fourth terminal state and never passes through `new`: a
    # correspondent hands over a finished post (0046), and nothing triaged it,
    # so recording `triaged` with a fabricated decision would be a lie. It is
    # out of `triage_pending()` and `write_pending()` by construction, because
    # both match a status by name rather than excluding one.
    status: Mapped[str] = mapped_column(String(20), default="new")
    triage_decision: Mapped[str | None] = mapped_column(String(20))
    triage_reason: Mapped[str | None] = mapped_column(Text)
    # Triage's ranking signal; the writer works candidates highest-first (nullable
    # until triaged). Unbounded above — see schemas.TriageResult.quality_score.
    rank_score: Mapped[float | None] = mapped_column()
    # What the research agent gathered for this story: fetched URLs + notes, kept
    # for observability and to build the article's "further reading" section.
    research_notes: Mapped[Any | None] = mapped_column(JSONB)
    topics: Mapped[list[str]] = mapped_column(JSONB, default=list)
    centroid: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    item_count: Mapped[int] = mapped_column(default=0)
    first_item_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_item_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Ordered so that `story.items | first` IS the primary item (0047): the card
    # title, the source name, the snippet and the destination are all read off
    # the same row, and two renders of one card cannot disagree. Without an
    # order_by this was physical row order, which is not a guarantee at all.
    items: Mapped[list[SourceItem]] = relationship(
        back_populates="story", order_by=PRIMARY_ITEM_ORDER
    )
    posts: Mapped[list[Post]] = relationship(back_populates="story")


@dataclass(frozen=True)
class PostKind:
    """Everything a kind has to declare before a post of it can exist.

    One table rather than a filter per stage, because the alternative is what
    `qa_pending()` used to be: `kind != "aggregate"`, which silently enrolled
    every future kind in main-model review (0046). `Post.kind` refuses a value
    that is not in `POST_KINDS`, so a new kind cannot reach the database without
    answering every question here.
    """

    #: Renders itself at /post/{id} and carries `sections` (0047). False = it
    #: stores an `href` and carries no body. The database enforces the invariant
    #: itself, over `href` and `sections` rather than over `kind`
    #: (ck_posts_body_iff_self_rendering); this is the producer-side decision.
    renders_itself: bool
    #: Enters `qa_pending()`: the main model reviews and may demote it.
    reviewed: bool
    #: Enters `score_pending()`: the profile gives it an affinity score.
    scored: bool
    #: Its feed card reads the story's items, so the feed batch-loads them (and
    #: their sources) for this kind. An article card reads the denormalized
    #: `banner_url` and never touches items; an aggregate reads its primary item;
    #: a filed card reads the source behind it to name its correspondent.
    needs_items: bool


POST_KINDS: dict[str, PostKind] = {
    "article": PostKind(
        renders_itself=True, reviewed=True, scored=True, needs_items=False
    ),
    # An identity-only cluster card. Nothing was generated, so there is nothing
    # to review; it still ranks against the profile like any other feed unit.
    "aggregate": PostKind(
        renders_itself=False, reviewed=False, scored=True, needs_items=True
    ),
    # Filed by a correspondent, finished on arrival (0046). It touches no
    # pipeline stage at first: scoring is the first to integrate later, QA much
    # later, and neither is a decision to make before there is a filed post to
    # look at.
    "filed": PostKind(
        renders_itself=False, reviewed=False, scored=False, needs_items=True
    ),
}


class Post(Base):
    """A feed content unit — ALL feed content is a post (decided 2026-07-18), so
    every visible unit has one id from triage verdict to publication/archival.
    `kind`: `article` = the long-form written post (title/summary/sections filled);
    `aggregate` = identity-only row for a cluster card — no stored content, the
    card renders from the story's items at read time (canonical minimum).
    Micro-posts and minigames are later Phase 3 work (definitions in spec §12).
    At most one published post per story at any time. `sections` is the
    typed-section data of spec §6."""

    __tablename__ = "posts"
    __table_args__ = (
        # A post carries a body exactly when it renders itself (0047). Not a rule
        # about kinds: it is written over the two columns it constrains, so a kind
        # that arrives later is covered without editing it.
        # `jsonb_typeof` is not decoration: `jsonb_array_length` RAISES on a JSON
        # scalar rather than returning false, and a constraint that errors is
        # harder to diagnose than one that rejects.
        CheckConstraint(
            "(href IS NULL) = "
            "(jsonb_typeof(sections) = 'array' AND jsonb_array_length(sections) > 0)",
            name="ck_posts_body_iff_self_rendering",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"))
    kind: Mapped[str] = mapped_column(String(20), default="article")
    # Where the card and the canonical URL both go (0047). NULL = this post
    # renders itself; an aggregate holds its primary item's URL. Stored rather
    # than derived so the CHECK can exist: a constraint cannot join to
    # source_items to work out what an aggregate's destination would be.
    href: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    difficulty: Mapped[str | None] = mapped_column(String(20))
    topics: Mapped[list[str]] = mapped_column(JSONB, default=list)
    sections: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    reading_time_minutes: Mapped[int] = mapped_column(default=1)
    # Denormalized feed-card banner (first image among the story's items), stamped at
    # write time so the feed renders an article card without loading its story's items
    # — the "derivation isn't cheap, so store the minimum" call (see web.app._feed_page).
    banner_url: Mapped[str | None] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(Text)
    quality_score: Mapped[float | None] = mapped_column()  # set by the qa stage
    # How well this post matches the interest profile, written by the `score`
    # stage (recommend.scorers). Deliberately WITHOUT the freshness term: that is
    # applied in the feed query, so a post's stored score doesn't need rewriting
    # as it ages. NULL = never scored, which the feed ranks as neutral (0).
    affinity_score: Mapped[float | None] = mapped_column()
    # Per-signal breakdown behind `affinity_score` — the debugging surface, and
    # what the "why am I seeing this" explanation will read when it lands.
    score_components: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="published")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # When this post becomes visible; NULL = immediately (0046). A correspondent
    # reads a week's content once and creates all five posts from that one read,
    # each with its own date, so lunch does not depend on a nightly job firing.
    # The feed filters on it AND sorts on it: without the second half, five posts
    # created on Sunday all carry Sunday's `generated_at` and Friday's card
    # arrives five days stale.
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When this post stops being visible; NULL = never. The mirror of
    # `publish_at`, and the same mechanism: one clause in the feed query, no job.
    # Today's lunch menu is worth reading until the kitchen closes and is purely
    # historical after (TODO.md). Expiry removes it from the FEED only - the post
    # is still at its own URL and the week is still on the correspondent's page,
    # which is where standing content lives. Nothing archives it (0046).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the post leaves "published" (rewrite supersession or QA demote);
    # archived posts older than llm_log_retention_days are pruned with their calls.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # User-set retention override: a pinned post and its stamped llm_calls are
    # never auto-pruned (e.g. keep a superseded draft for later comparison).
    pinned: Mapped[bool] = mapped_column(default=False)

    story: Mapped[Story] = relationship(back_populates="posts")
    audios: Mapped[list[PostAudio]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )

    @validates("kind")
    def _validate_kind(self, _key: str, value: str) -> str:
        """Reject a kind nobody has decided the destination of.

        Fires on assignment, not on load, so existing rows are unaffected and the
        failure lands on whoever wrote the producer. Same move as
        `chat_tools.validate_registry`: the property is checked mechanically over
        the table instead of being remembered."""
        if value not in POST_KINDS:
            raise ValueError(
                f"unknown post kind {value!r}: add it to models.POST_KINDS with a "
                "decision about whether it renders itself (0047)"
            )
        return value


class Correspondent(Base):
    """The configuration half of a correspondent (0046); the code half is the
    plugin the registry resolves from `slug`.

    Same split `sources` already uses: a row names the thing and carries its
    settings, an in-tree implementation does the work. A row with no plugin is a
    correspondent that is configured but not installed, which is what an external
    service would be if any existed.

    **No credential columns.** 0046 designed two, encrypted at rest, and both
    belong entirely to the external-service path, which that same decision
    defers with no members. Adding an encryption dependency and a key in `.env`
    for a class that is empty is more than the job needs; two nullable columns
    are a cheap migration on the day something outside this repository files a
    post.
    """

    __tablename__ = "correspondents"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Permanent identity, and what `/c/<slug>/` is built from. Renaming it moves
    # every URL the correspondent owns, which is why `label` exists separately.
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    label: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(default=True)
    # Whatever the plugin declares configurable. Opaque to core, exactly as
    # `sources.config` is to everything but its adapter.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PostAudio(Base):
    """One TTS narration of a post version, in one voice. A post can have several
    (one per catalog voice the reader has generated), so the row is keyed by
    (post_id, voice) — the reader picks a voice on the page and each is synthesized
    and cached independently. Article posts only — aggregate cards render from
    their items and aren't narrated.

    The MP3 lives on disk under settings.audio_dir (bind-mounted to the host); this
    row is its metadata plus the `script_hash` that lets the narrate stage skip an
    up-to-date voice and re-narrate after a QA revision changes the sections. Kept
    separate from Post so an un-narrated post is simply a missing row.

    The `script` column is deliberately NOT stored: today it is derived from the
    post (tts.script.build_script), so only its hash is canonical. When the future
    LLM preprocessing pass emits a marked-up (non-derivable) script, it gets its
    own column here."""

    __tablename__ = "post_audio"
    __table_args__ = (
        UniqueConstraint("post_id", "voice", name="uq_post_audio_post_voice"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # ondelete CASCADE so the retention prune's bulk `delete(Post)` (Core, no ORM
    # cascade) doesn't hit an FK violation on an archived post's audio rows.
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), index=True
    )
    voice: Mapped[str] = mapped_column(Text)  # Fish reference_id (catalog voice id)
    # pending -> ready | failed. `ready` means `path` points at a playable file.
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # Path relative to audio_dir (e.g. "247-<voice>.opus"); served at /media/<path>.
    path: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)  # Fish backend header, e.g. "s2.1-pro-free"
    # Provenance of how this file was generated: which provider synthesized it and
    # the exact generation params (temperature/top_p/prosody) that were used. The
    # narrate stage re-synthesizes when these drift, not only when the script does.
    provider: Mapped[str | None] = mapped_column(Text)  # e.g. "fish"; future: "local"
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    audio_format: Mapped[str] = mapped_column(String(10), default="opus")
    duration_seconds: Mapped[float | None] = mapped_column()
    char_count: Mapped[int] = mapped_column(default=0)  # script length (cost signal)
    # sha256 of the narration script; the stage re-narrates when it changes.
    script_hash: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    post: Mapped[Post] = relationship(back_populates="audios")


class Feedback(Base):
    """One reader signal — and the canonical record of the whole recommendation
    system's learning.

    The `InterestProfile` is a *derived cache*: it is recomputed by replaying
    this log (see `recommend.profile.rebuild`). That is what makes undo exact
    (delete the row, rebuild) and lets the learning rate or half-life be retuned
    retroactively instead of leaving a mistrained profile behind.

    Replay therefore may not depend on anything mutable, so each event carries
    its own payload: `embedding` and `topics_snapshot` are what the reader was
    reacting to *at the moment they reacted*. Stories keep absorbing items and
    posts get rewritten and eventually pruned; "the embedding of the thing I
    liked" is genuinely not "the embedding of that story today". `post_id` is a
    link for the UI, not an input to replay — it goes NULL when the retention
    prune removes an archived post, and replay is unaffected.

    `nl_text` and `parsed_intent` are canonical for the same reason: the
    `llm_calls` row holding the verbatim prompt is pruned after
    `llm_log_retention_days`, but a natural-language statement keeps shaping the
    profile long after that."""

    __tablename__ = "feedback"
    __table_args__ = (Index("ix_feedback_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # like | dislike | save | more_topic | less_topic | set_topic | hide_source
    # | block_keyword | unblock_keyword | nl_feedback
    kind: Mapped[str] = mapped_column(String(30))
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), index=True
    )
    # Canonical topic SLUG for more_topic / less_topic / set_topic — the topic's
    # permanent identity, not its current spelling, so a rename can never detach a
    # signal from the weight it was recorded to feed. `topics_snapshot` and
    # `parsed_intent["topics"][].topic` hold slugs for the same reason.
    topic: Mapped[str | None] = mapped_column(Text)
    # The number the reader typed, for set_topic: an ABSOLUTE weight, not a step.
    # Every other signal is relative and decays; this one is a stated position, so
    # replay treats it as an anchor that discards whatever came before it.
    value: Mapped[float | None] = mapped_column()
    # The blocked substring, for block_keyword / unblock_keyword. Not a `topic`:
    # a keyword is matched literally against a post's title and summary, never
    # canonicalized into the vocabulary, so the two must not share a column.
    # `unblock_keyword` exists as its own event because a keyword block can
    # originate from a natural-language statement — removing it must not require
    # deleting that statement and the topic weights it also set.
    keyword: Mapped[str | None] = mapped_column(Text)
    # Sources that contributed to what was reacted to; for hide_source, the one
    # being hidden. A snapshot like the rest of the payload — no FK.
    source_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    topics_snapshot: Mapped[list[str]] = mapped_column(JSONB, default=list)
    difficulty: Mapped[str | None] = mapped_column(String(20))
    nl_text: Mapped[str | None] = mapped_column(Text)
    parsed_intent: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class InterestProfile(Base):
    """Singleton (id=1) cache of the feedback log, replayed by
    `recommend.profile.rebuild`. Nothing here is authoritative — deleting the row
    loses nothing but the time it takes to replay.

    It is stored rather than computed per request because the scorer runs in the
    worker and the feed reads in the web process, and because `rebuilt_at` /
    `event_count` make staleness visible instead of implicit.

    Weights are keyed by a topic's permanent SLUG — stable across a rename, unlike
    the labels stored on stories and posts, which is why reading a weight for a
    post goes through `recommend.topics.slug_index` — and by source id as a string
    (JSONB keys are always strings)."""

    __tablename__ = "interest_profile"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    liked_centroid: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    disliked_centroid: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    topic_weights: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict)
    source_weights: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict)
    difficulty_weights: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict)
    blocked_keywords: Mapped[list[str]] = mapped_column(JSONB, default=list)
    blocked_sources: Mapped[list[int]] = mapped_column(JSONB, default=list)
    # The reader's own words about what they want (cold start, and editable
    # later). Stored for display; its effect reaches the profile through the
    # feedback row that carries its parsed intent.
    intent_statement: Mapped[str | None] = mapped_column(Text)
    event_count: Mapped[int] = mapped_column(default=0)
    rebuilt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Topic(Base):
    """The canonical topic vocabulary the interest profile learns weights over.

    Topic tags are LLM free text, and free text drifts: "astrophysics",
    "astronomy" and "space" arrive as three unrelated keys, so weights learned
    against them are weights learned against noise. This table is the closed set;
    `recommend.topics.resolve` maps whatever the model emits onto it in code —
    the same "offer a closed set, enforce it outside the model" pattern already
    used for media URLs and citations.

    `slug` is the topic's PERMANENT identity: assigned once from the label the
    entry was created with, and never moved again. Everything keyed to a topic is
    keyed to it — interest weights now, relations between topics later — because a
    key derived from the current label silently re-keys the topic (and dangles
    every weight pointing at it) the first time someone corrects a spelling.
    `recommend.topics.rename` therefore changes only `label`, and adds the new
    spelling to `aliases` so it resolves back here.

    `Story.topics` / `Post.topics` hold canonical `label` strings (not ids), so
    templates and API payloads render them directly. A rename therefore rewrites
    the referencing rows too — `recommend.topics.rename` does both in one
    transaction, and it is a rare, deliberate admin action. Going from one of those
    stored labels back to the identity its weight lives under is
    `recommend.topics.slug_index` / `slug_for`, never a bare `slugify`.

    `aliases` records the slugified raw phrasings that resolved here, so a repeat
    of a known phrasing short-circuits the embedding lookup. Raw model output is
    not lost either way: it is in the triage call's `llm_calls` row."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    label: Mapped[str] = mapped_column(Text)
    # Embedded once at creation; the nearest-neighbour lookup that folds new
    # phrasings into existing topics needs it. Nullable so a topic can still be
    # created when the embed model is down (backfilled later).
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    aliases: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Voice(Base):
    """A selectable narration voice (the catalog). Lives in the DB rather than in
    code so voices and their generation parameters can be managed as data and, in
    the future, differ per provider without a code change.

    `id` is the catalog key and — for the Fish provider — equals the Fish
    `reference_id`, so it stays the value stored in `post_audio.voice`, the
    on-disk filename, and the `?voice=` query param. `provider_voice_id` lets a
    provider address the voice by a different id (defaults to `id`).

    `params` is a provider-agnostic JSON bag of generation controls
    (`{temperature, top_p, prosody: {speed, volume}}` for Fish); each provider
    reads the keys it understands, so a future local model can carry its own
    params here with no schema change. The default voice is the lowest
    `sort_order` enabled row (overridable via settings.tts_default_voice)."""

    __tablename__ = "voices"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text, default="fish")
    provider_voice_id: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    sort_order: Mapped[int] = mapped_column(default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# Stages whose calls belong to ONE post generation (post-scoped provenance).
# Calls in other stages that carry a story_id (triage) are story-level, shared
# by every version of the story's post. Used by the /api/posts/{id}/llm-calls
# filter and by the retention prune's pinned-post protection — the two must
# agree so a pinned post's provenance page stays complete.
POST_SCOPED_STAGES = ("condense", "research", "write", "qa")


# What kind of thing a queued job IS. Two very different consumers read this and
# they must not drift: the history prune keeps each class for a different window,
# and the admin queue folds the two plumbing classes out of the default view. If
# they disagreed, the page would hide rows the prune keeps (invisible history) or
# show rows the prune has already dropped as if they were the whole story.
#
# `scheduler` is procrastinate's periodic entry point — the task the cron fires,
# whose only job is to defer the real one. It carries a raw unix `timestamp` arg
# and no outcome of its own, so it is plumbing in the strictest sense: every tick
# writes one of these next to the row that did the work.
JOB_CLASS_SCHEDULER = "scheduler"
JOB_CLASS_MAINTENANCE = "maintenance"
JOB_CLASS_INGEST = "ingest"
JOB_CLASS_WORK = "work"

# Classes the queue page folds away by default. Both are self-firing housekeeping
# with no editorial meaning: seeing them is opt-in, not the default reading.
JOB_PLUMBING_CLASSES = (JOB_CLASS_SCHEDULER, JOB_CLASS_MAINTENANCE)

_MAINTENANCE_TASKS = frozenset(
    {
        "episteme.govern_resources",
        "episteme.recover_stalled_jobs",
        "episteme.prune_job_history",
    }
)
_INGEST_TASKS = frozenset({"episteme.ingest_all", "episteme.ingest_source"})

# Terminal statuses that are not a success, and the ones that mean the job has
# not finished at all. Here rather than in the worker because the web process
# reads them too (the queue's failure filter and its maintenance summary), and
# web deliberately does not import worker modules at import time — doing so would
# register the periodic tasks in a process that never runs them.
FAILURE_STATUSES = ("failed", "cancelled", "aborted")
LIVE_STATUSES = ("todo", "doing", "aborting")


def job_class(task_name: str) -> str:
    """Classify a procrastinate task name. Prefix-matched on `scheduled_` first:
    a periodic entry point is plumbing whatever it goes on to defer, so
    `scheduled_ingest` is a scheduler row and not an ingest one."""
    bare = task_name.removeprefix("episteme.")
    if bare.startswith("scheduled_"):
        return JOB_CLASS_SCHEDULER
    if task_name in _MAINTENANCE_TASKS:
        return JOB_CLASS_MAINTENANCE
    if task_name in _INGEST_TASKS:
        return JOB_CLASS_INGEST
    return JOB_CLASS_WORK


class ChatMessage(Base):
    """One turn of the reader's conversation with the assistant (`llm.chat`).

    Durable because approval is: a `role="proposal"` row is a write the model
    asked to perform and has NOT performed, and that question has to survive a
    reload, a restart, and the reader wandering off for an hour. Stream state
    could not carry it, which is the whole reason this table exists rather than
    an in-memory deque.

    A proposal stores the *call*, not its rendering: `tool_name`, `tool_args`
    and `tool_call_id` reconstruct the exact assistant/tool message pair the
    model sees when the loop resumes, so an approved write continues a real
    conversation instead of a paraphrase of one. The sentence on the approval
    card is `chat_tools.describe(args)`, computed at render time — derivable, so
    not stored. `content` is NULL while a proposal is pending and holds the
    tool's result (or the rejection note) afterwards.

    There is no `seq`: `id` already orders a session's rows by insertion, which
    is conversation order. `llm_calls` still holds the raw exchange for
    provenance, joined by `chain_id`, and is prunable — this table is not."""

    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_session", "session_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36))  # uuid4, one per conversation
    role: Mapped[str] = mapped_column(String(20))  # user | assistant | proposal
    content: Mapped[str | None] = mapped_column(Text)
    # What the reader was looking at when they typed. A link for context and for
    # the UI; goes NULL when the retention prune removes an archived post.
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id", ondelete="SET NULL"))
    tool_name: Mapped[str | None] = mapped_column(String(50))
    tool_args: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    proposal_status: Mapped[str | None] = mapped_column(String(20))  # pending|approved|rejected
    chain_id: Mapped[str | None] = mapped_column(String(36))  # -> llm_calls.chain_id
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LlmCall(Base):
    """One gateway call: full request/response plus timing, for the admin
    provenance view. Written best-effort by llm.observe — never blocks the
    pipeline. story_id is a plain int (no FK) so logging can't fail a write."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    role: Mapped[str] = mapped_column(String(10))  # main | fast | embed
    model: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))  # chat | tool-chat | embed
    stage: Mapped[str | None] = mapped_column(String(20))  # embed|triage|condense|write|qa
    story_id: Mapped[int | None] = mapped_column(index=True)
    # The post generation this call belongs to. qa stamps it at call time; the
    # write stage stamps condense/write calls right after the post row lands
    # (the post doesn't exist yet while they run). NULL = story-level work
    # (triage) that is shared provenance across every version of a post.
    post_id: Mapped[int | None] = mapped_column(index=True)
    # Groups the write-side calls of ONE generation attempt (uuid, set at call
    # time via llm_context). The post stamp targets exactly this attempt, so a
    # failed attempt's calls can never be swept into a later post's provenance.
    attempt_id: Mapped[str | None] = mapped_column(String(36))
    # User-set retention override for calls with no post row to pin through —
    # a failed attempt's write-side calls (post_id NULL), pinned by attempt_id.
    # Calls stamped to a pinned post are protected via the post, not this flag.
    pinned: Mapped[bool] = mapped_column(default=False)
    # Tool loops store per-row message DELTAS: rows sharing a chain_id are one
    # conversation; full transcript = concat(request.messages + response) by seq.
    chain_id: Mapped[str | None] = mapped_column(String(36))
    seq: Mapped[int | None] = mapped_column()
    duration_ms: Mapped[int] = mapped_column(default=0)
    prompt_tokens: Mapped[int | None] = mapped_column()
    completion_tokens: Mapped[int | None] = mapped_column()
    request: Mapped[Any | None] = mapped_column(JSONB)  # {"messages": [...], ...}
    response: Mapped[Any | None] = mapped_column(JSONB)  # raw assistant message
    error: Mapped[str | None] = mapped_column(Text)


class PipelineRun(Base):
    """One orchestrator pass with per-stage counters — turns 'did last night's
    run work?' into a table row instead of log archaeology."""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # running|succeeded|failed|skipped|paused — paused runs left work behind on
    # purpose; deferring the pipeline again picks it up (stages are data-driven).
    status: Mapped[str] = mapped_column(String(20), default="running")
    stages: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # {"embed": 323, ...}
    error: Mapped[str | None] = mapped_column(Text)


class AppState(Base):
    """Key/value control flags (e.g. the pipeline pause switch) — persistent so a
    future resource governor can flip them via the API and worker restarts keep
    honoring them."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --- Benchmarking (docs/benchmarks/plan.md, 0039) ---------------------------------
#
# Deliberately NOT pruned by prune_job_history. These rows are small and their
# entire value is in being old: a number from March is what a number from
# September disagrees with.


class BenchmarkFixture(Base):
    """A frozen prompt, replayed to measure throughput on the work Episteme really
    does (a synthetic 4k prompt overstates prefill by 3-4x).

    **Snapshotted, not referenced.** `llm_calls` is pruned on a tiered retention
    (0032), so a fixture pointing at a `chain_id` would rot the moment its source
    aged out, taking every comparison against it along. The source ids are kept
    for provenance only and are allowed to dangle."""

    __tablename__ = "benchmark_fixture"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # replay | synthetic
    stage: Mapped[str | None] = mapped_column(String(20))  # captured from write|qa|triage
    source_chain_id: Mapped[str | None] = mapped_column(String(36))
    source_story_id: Mapped[int | None] = mapped_column()
    # Flattened, tool-free, ending on a user turn. Full research text, not a token
    # count: a later quality run has to be able to replay this verbatim.
    messages: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # ADVISORY. Counted with one model's tokenizer, and models disagree - so this
    # is never the x-axis of a chart. Per-sample `prompt_n` is.
    prompt_tokens: Mapped[int | None] = mapped_column()
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    notes: Mapped[str | None] = mapped_column(Text)


class BenchmarkRun(Base):
    """One execution of one scenario over one or more models.

    No `ctx_size` column, on purpose: context size is per model, not per run, and
    it is recoverable exactly from the argv stored on each sample."""

    __tablename__ = "benchmark_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|succeeded|failed|cancelled
    executor: Mapped[str] = mapped_column(String(10), default="worker")  # worker | host
    scenario: Mapped[str] = mapped_column(String(20))  # quick|longctx|ladder|sweep
    fixture_id: Mapped[int | None] = mapped_column(
        ForeignKey("benchmark_fixture.id", ondelete="SET NULL")
    )
    models: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # Everything the launch form chose: reps, predict, ladder rungs, sweep
    # variants. The run row IS the parameter record, which is what lets the job
    # take a single int and stay inside procrastinate's argument conventions.
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    llama_build: Mapped[str | None] = mapped_column(Text)
    env: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # /resources at start
    env_end: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # ... and at finish
    # Started clean, ended dirty: the 2026-08-15 Warframe case. Stays visible in
    # history with a label rather than being hidden, and is never a baseline.
    contaminated: Mapped[bool] = mapped_column(default=False)
    # Live state, overwritten as the run goes, read by the SSE progress endpoint.
    # Not a result: nothing reads it once the run has finished.
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # The page sets it; the runner reads it between chunks and drops the HTTP
    # connection, which is the only abort llama-server offers.
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text)

    samples: Mapped[list[BenchmarkSample]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="BenchmarkSample.id"
    )
    fixture: Mapped[BenchmarkFixture | None] = relationship()


class BenchmarkSample(Base):
    """One model, one variant, one repetition.

    `args` is the highest-value column here. Throughput says THAT something
    regressed; only the effective argv says what changed - and the regression this
    whole feature exists for (an `ngl` override silently defeating `fit = on` for
    months) is invisible in every other column."""

    __tablename__ = "benchmark_sample"
    # The slot a measurement occupies. `rung` is part of it because the ladder
    # takes one sample per rung and they are all rep 0 - without it, a five-rung
    # ladder violates this constraint on its second point. NULLS NOT DISTINCT
    # (PG15+) is what keeps the guard real for every other scenario, where `rung`
    # is NULL and Postgres would otherwise treat every row as unique.
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "model",
            "variant",
            "rep",
            "rung",
            name="uq_benchmark_sample_slot",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("benchmark_run.id", ondelete="CASCADE"))
    model: Mapped[str] = mapped_column(Text)
    variant: Mapped[str] = mapped_column(String(40), default="")  # sweep label, else ""
    rep: Mapped[int] = mapped_column(default=0)  # 0 is the warmup, stored anyway
    rung: Mapped[int | None] = mapped_column()  # ladder: the length ASKED for
    prompt_n: Mapped[int] = mapped_column(default=0)  # ... the one MEASURED
    # Prompt tokens served from cache. A cold-prefill measurement with cache_n > 0
    # is void, and this column is what proves a ladder point was not.
    cache_n: Mapped[int] = mapped_column(default=0)
    prefill_ms: Mapped[int] = mapped_column(default=0)  # llama.cpp `timings`,
    decode_ms: Mapped[int] = mapped_column(default=0)  # never derived from wall time
    decode_tokens: Mapped[int] = mapped_column(default=0)
    wall_ms: Mapped[int] = mapped_column(default=0)  # wall - prefill - decode = overhead
    load_ms: Mapped[int | None] = mapped_column()  # model swap, first sample of a model
    accept_pct: Mapped[float | None] = mapped_column()  # MTP draft acceptance
    vram_free_mb: Mapped[int | None] = mapped_column()  # while THIS model is resident
    args: Mapped[list[Any] | None] = mapped_column(JSONB)  # /v1/models[].status.args
    # [[prompt_n, tok_s], ...] instantaneous, differenced from `prompt_progress`.
    prefill_series: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # [[token_index, elapsed_ms], ...] bucketed. Columns, not a fourth table: the
    # only consumer reads the whole series at once, exactly like pipeline_runs.stages.
    decode_series: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # One model failing must not lose the other's numbers, so a failure is a value
    # on the sample rather than an exception that ends the run.
    error: Mapped[str | None] = mapped_column(Text)

    run: Mapped[BenchmarkRun] = relationship(back_populates="samples")
