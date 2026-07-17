from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Dimension of the `embed` model role output. 1024 = native dim of
# Octen-Embedding-0.6B; larger models (4B = 2560) are truncated + re-normalized
# by the gateway (Matryoshka). Changing this requires a vector-column migration
# (see bootstrap.ADDITIVE_MIGRATIONS) and re-embedding everything.
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
    articles: Mapped[list[Article]] = relationship(back_populates="story")


class Article(Base):
    """A generated article; `sections` is the typed-section data of spec §6."""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    difficulty: Mapped[str] = mapped_column(String(20))
    topics: Mapped[list[str]] = mapped_column(JSONB, default=list)
    sections: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    reading_time_minutes: Mapped[int] = mapped_column(default=1)
    model_used: Mapped[str | None] = mapped_column(Text)
    quality_score: Mapped[float | None] = mapped_column()  # populated by verify (Phase 4)
    status: Mapped[str] = mapped_column(String(20), default="published")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    story: Mapped[Story] = relationship(back_populates="articles")
