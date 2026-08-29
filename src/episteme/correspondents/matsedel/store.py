"""Storing a week that was read, and reading weeks back out.

A re-read of a week replaces its lines rather than adding a second copy, the same
rule filing follows for the posts (0046): a menu corrected on Tuesday morning is
the same week saying something truer, not two weeks.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...models import Source
from .models import MatsedelDish, MatsedelWeek
from .readers import WeekMenu
from .tags import HIDDEN, tag_of

log = logging.getLogger("episteme.correspondents.matsedel")


async def store_week(session: AsyncSession, source: Source, menu: WeekMenu) -> MatsedelWeek:
    """Upsert one restaurant's week. The caller commits.

    **A day that comes back with no menu does not replace a day that had one.**
    Kalasboden took week 35 down mid-week and served
    `Meny for denna dag saknas.` for every day; the re-read overwrote the real
    menu with five placeholders and the five filed posts degraded with it
    (watched 2026-08-29). A menu that was read was true when it was read, and a
    site saying nothing today is not the site correcting itself.

    "No menu" is `tags.tag_of` calling every one of the day's lines `HIDDEN`,
    which is the same table the pages read, so a placeholder that is invisible on
    `/c/matsedel` is also one that cannot overwrite. A day missing from the read
    entirely counts the same way. This is per DAY rather than per week, so a site
    that has published Monday to Thursday and not yet Friday stores four days and
    keeps whatever Friday it already had.

    What this cannot do is notice a page that replaces a real menu with a
    different real menu. That is the site correcting itself, and it is meant to
    win.
    """
    week = (
        await session.execute(
            select(MatsedelWeek).where(
                MatsedelWeek.source_id == source.id,
                MatsedelWeek.week_key == menu.week_key,
            )
        )
    ).scalars().first()
    stored: dict[date, list[str]] = {}
    if week is None:
        week = MatsedelWeek(
            source_id=source.id, week_key=menu.week_key, monday=menu.monday
        )
        session.add(week)
        await session.flush()
    else:
        for serve_date, text in (
            await session.execute(
                select(MatsedelDish.serve_date, MatsedelDish.text)
                .where(MatsedelDish.week_id == week.id)
                .order_by(MatsedelDish.position)
            )
        ).all():
            stored.setdefault(serve_date, []).append(text)
        # Delete and rewrite rather than diff: positions shift when a line is
        # added in the middle, so a diff would have to renumber anyway, and the
        # unique constraint on (week_id, position) makes a partial rewrite
        # collide with itself halfway through. A kept day is rewritten too, from
        # what was just read out, so one sequence still numbers the whole week.
        await session.execute(
            delete(MatsedelDish).where(MatsedelDish.week_id == week.id)
        )
        week.monday = menu.monday

    site = (source.config or {}).get("site")
    incoming = {day.served_on: list(day.lines) for day in menu.days}
    position = 0
    kept: list[date] = []
    for serve_date in sorted(set(incoming) | set(stored)):
        lines = incoming.get(serve_date, [])
        if not any(tag_of(site, line) != HIDDEN for line in lines) and stored.get(
            serve_date
        ):
            lines = stored[serve_date]
            kept.append(serve_date)
        for line in lines:
            session.add(
                MatsedelDish(
                    week_id=week.id,
                    serve_date=serve_date,
                    position=position,
                    text=line,
                )
            )
            position += 1
    await session.flush()
    log.info(
        "Stored %s %s: %d line(s) over %d day(s)%s",
        source.name,
        menu.week_key,
        position,
        len(menu.days),
        f", kept {len(kept)} day(s) the read had no menu for" if kept else "",
    )
    return week


async def week_of(session: AsyncSession, monday: date) -> list[MatsedelWeek]:
    """Every restaurant's row for one week, with its lines and its source loaded."""
    return list(
        (
            await session.execute(
                select(MatsedelWeek)
                .options(
                    selectinload(MatsedelWeek.dishes),
                    selectinload(MatsedelWeek.source),
                )
                .where(MatsedelWeek.monday == monday)
                .order_by(MatsedelWeek.source_id)
            )
        ).scalars()
    )


async def stored_weeks(session: AsyncSession, mondays: Sequence[date]) -> set[date]:
    """Which of these weeks anything is stored for.

    One query rather than one per week, because the only caller asks about the
    week before and the week after at the same time, to decide whether to offer
    an arrow at all. An arrow to a week nothing was read for lands on a page
    that says nothing was read for it, which is a dead end the reader has to
    back out of. This asks about EXACTLY the weeks it is handed: a gap in the
    history stops the arrows rather than jumping over it, so what the arrow
    means stays "the week next to this one".
    """
    if not mondays:
        return set()
    return set(
        (
            await session.execute(
                select(MatsedelWeek.monday)
                .where(MatsedelWeek.monday.in_(list(mondays)))
                .distinct()
            )
        ).scalars()
    )


async def latest_monday(session: AsyncSession, *, not_after: date | None = None) -> date | None:
    """The most recent week anything was stored for, at or before `not_after`."""
    stmt = select(MatsedelWeek.monday).order_by(MatsedelWeek.monday.desc()).limit(1)
    if not_after is not None:
        stmt = stmt.where(MatsedelWeek.monday <= not_after)
    return (await session.execute(stmt)).scalars().first()
