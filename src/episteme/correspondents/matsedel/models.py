"""Matsedel's own tables (0046).

A plugin may define tables, and they go in core's single Alembic history like any
other table, because every plugin currently imaginable ships in this repository.
`migrations/env.py` imports `episteme.correspondents` for exactly this reason: the
metadata has to know about a table before `--autogenerate` can see it.

**Why store the week at all**, when the posts are what the reader sees. A post is
archived and eventually pruned; the week is where lunch history lives, and it is
what `/c/matsedel` and `/c/matsedel/stats` read. Storing the scrape also means the
five weekday posts are minted from a row rather than from a live fetch, so a site
being down on Wednesday morning costs nothing.

**A line, not a dish.** The unit is one line of the restaurant's own menu, in the
order the restaurant wrote it. Deciding which lines are dishes and which are
labels ("BUFFE", "Dagens vegetariska", "Fran koket:") means a classifier whose
mistakes are invisible: a dropped line is a dish nobody was served. Lines with no
content at all - a bare number, a rule of dashes - are dropped by the reader, and
that is the whole of what a row's TEXT is filtered by.

Which is why `tags.tag_of` is applied on read and not here. A line tagged HIDDEN
is stored like any other and rendered nowhere, so retagging corrects every week
already stored. `store_week` reads the tag for one write-time decision only, and
it is a decision about what to KEEP: a day whose every incoming line is hidden
does not replace a day that had a menu.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...models import Base, Source


class MatsedelWeek(Base):
    """One restaurant's menu for one ISO week, as read on one day."""

    __tablename__ = "matsedel_weeks"
    __table_args__ = (
        # Re-reading a week corrects it in place rather than storing it twice,
        # which is the same rule filing follows for the posts (0046).
        UniqueConstraint("source_id", "week_key", name="uq_matsedel_weeks_source_week"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # One `sources` row per restaurant, all with `type_name="matsedel"`, so each
    # site keeps its own cooldown, cache validators and enabled flag.
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    # ISO year and week, `2026w35`. The same string the period key is built from,
    # and the reason it is stored rather than derived: a menu read on Sunday for
    # next week has no relationship to the date it was read on.
    week_key: Mapped[str] = mapped_column(String(10))
    # The week the menu covers, Monday. Every query that orders history uses this
    # rather than `week_key`, because `2026w9` sorts after `2026w35` as a string.
    monday: Mapped[date] = mapped_column(Date())
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    source: Mapped[Source] = relationship()
    dishes: Mapped[list[MatsedelDish]] = relationship(
        back_populates="week", cascade="all, delete-orphan", order_by="MatsedelDish.position"
    )


class MatsedelDish(Base):
    """One line of one day's menu, in the order the restaurant wrote it."""

    __tablename__ = "matsedel_dishes"
    __table_args__ = (
        UniqueConstraint("week_id", "position", name="uq_matsedel_dishes_week_position"),
        # The filing pass reads one day across every restaurant.
        Index("ix_matsedel_dishes_serve_date", "serve_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    week_id: Mapped[int] = mapped_column(
        ForeignKey("matsedel_weeks.id", ondelete="CASCADE")
    )
    # The date this line is served on, not a weekday index: a post is minted for a
    # date, and a weekday would have to be resolved against the week on every read.
    serve_date: Mapped[date] = mapped_column(Date())
    # Position within the WEEK, so it is unique with `week_id` alone and the
    # week's lines can be replayed in their original order across day boundaries.
    position: Mapped[int] = mapped_column()
    text: Mapped[str] = mapped_column(Text)

    week: Mapped[MatsedelWeek] = relationship(back_populates="dishes")
