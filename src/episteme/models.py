from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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
    hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256(canonical url)
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


class Story(Base):
    """A cluster of SourceItems about the same underlying event/paper (spec §4)."""

    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(primary_key=True)
    # new -> triaged (decision recorded) -> written | aggregated | skipped
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

    items: Mapped[list[SourceItem]] = relationship(back_populates="story")
    posts: Mapped[list[Post]] = relationship(back_populates="story")


class Post(Base):
    """A feed content unit — ALL feed content is a post (decided 2026-07-18), so
    every visible unit has one id from triage verdict to publication/archival.
    `kind`: `feature` = long-form article (title/summary/sections filled);
    `aggregate` = identity-only row for a cluster card — no stored content, the
    card renders from the story's items at read time (canonical minimum).
    Micro-posts and minigames are later Phase 3 work (definitions in spec §12).
    At most one published post per story at any time. `sections` is the
    typed-section data of spec §6."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"))
    kind: Mapped[str] = mapped_column(String(20), default="feature")
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    difficulty: Mapped[str | None] = mapped_column(String(20))
    topics: Mapped[list[str]] = mapped_column(JSONB, default=list)
    sections: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    reading_time_minutes: Mapped[int] = mapped_column(default=1)
    model_used: Mapped[str | None] = mapped_column(Text)
    quality_score: Mapped[float | None] = mapped_column()  # set by the qa stage
    status: Mapped[str] = mapped_column(String(20), default="published")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
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


class PostAudio(Base):
    """One TTS narration of a post version, in one voice. A post can have several
    (one per catalog voice the reader has generated), so the row is keyed by
    (post_id, voice) — the reader picks a voice on the page and each is synthesized
    and cached independently. Feature posts only — aggregate cards render from
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
