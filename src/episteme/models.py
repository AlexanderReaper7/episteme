from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Dimension of the `embed` model role output. 768 fits nomic-style embedders;
# revisit when the embed role is benchmarked in Phase 2 (column is nullable
# and unused until then).
EMBEDDING_DIM = 768


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
