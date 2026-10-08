"""Turning a stored week into the five posts a reader sees (0046).

One post per weekday carrying every restaurant's menu for that day. One post per
restaurant would put four cards in the feed every morning and turn the
correspondent into a source of volume; one post per week would put standing
content in the feed, which is the thing the feed is not for.

The five posts are minted from one read and each carries its own `publish_at` and
`expires_at`, so lunch does not depend on the pipeline's health, on the GPU, or on
anything running that morning. A site being down on Wednesday costs nothing,
because Wednesday's post came from Monday's read.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...models import Correspondent, Post, Source
from ..filing import FiledItem, file_post
from .models import MatsedelWeek
from .readers import SERVED_DAYS
from .tags import DISH, HIDDEN, tag_of
from .store import week_of

log = logging.getLogger("episteme.correspondents.matsedel")

SLUG = "matsedel"

#: Serving hours, local time. A menu is worth reading from the morning until the
#: kitchens close, and is purely historical after; the week is still on
#: `/c/matsedel` either way, which is where standing content lives.
DEFAULT_PUBLISH_HOUR = 6
DEFAULT_EXPIRE_HOUR = 14


#: What the reader's clock says. Matsedel is four restaurants in one town, so its
#: opening hours are in the same zone the rest of the UI is rendered in.
def _zone() -> ZoneInfo:
    return ZoneInfo(settings.display_timezone)


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour=hour), tzinfo=_zone())


def summary_line(source: Source, lines: list[str]) -> str:
    """`Koppargrillen: Pannbiff med pepparsas` - one restaurant's headline dish.

    The first line `tags.tag_of` calls a dish, because a label introduces the
    dishes under it rather than naming one and a card built from one would say
    `Fran Buffe:` every day of the year. This picks what the SUMMARY quotes;
    every line the restaurant wrote is stored and rendered on the page.

    Takes the `Source` rather than its name because the tag table is keyed on the
    site, and passing both would be two arguments that must not disagree.
    """
    site = (source.config or {}).get("site")
    for line in lines:
        if tag_of(site, line) == DISH:
            return f"{source.name}: {line}"
    # Nothing but labels. Quoting one beats dropping a kitchen that did publish.
    # A HIDDEN line never gets here: `file_week` drops those before it counts a
    # kitchen as having published at all.
    return f"{source.name}: {lines[0]}" if lines else source.name


def day_href(served_on: date) -> str:
    """Where one day's post points, and where its notification links to (0047).

    Shared with `tasks.matsedel_notify`, which finds the day's post by this exact
    string. Two f-strings in two modules would be one silent rename away from a
    notification that never fires and a job history that says it ran.
    """
    return f"/c/{SLUG}#day-{served_on}"


def _title(served_on: date) -> str:
    # The day number is interpolated rather than formatted, because the strftime
    # directive for an unpadded day is `%-d` on glibc and `%#d` on Windows, and
    # this code runs on both.
    return f"Lunch on {served_on:%A} {served_on.day} {served_on:%B}"


async def _config(session: AsyncSession) -> dict:
    row = (
        (await session.execute(select(Correspondent).where(Correspondent.slug == SLUG)))
        .scalars()
        .first()
    )
    return dict(row.config or {}) if row is not None else {}


async def file_week(session: AsyncSession, monday: date) -> list[Post]:
    """File one post per weekday of `monday`'s week. The caller commits.

    A day no restaurant has a menu for gets no post, rather than a card that says
    nothing. Re-filing a day upserts, so correcting a menu mid-week does not mint
    a second Tuesday.
    """
    weeks = await week_of(session, monday)
    if not weeks:
        return []
    config = await _config(session)
    publish_hour = int(config.get("publish_hour", DEFAULT_PUBLISH_HOUR))
    expire_hour = int(config.get("expire_hour", DEFAULT_EXPIRE_HOUR))

    posts: list[Post] = []
    for offset in range(SERVED_DAYS):
        served_on = monday + timedelta(days=offset)
        day: list[tuple[Source, MatsedelWeek, list[str]]] = []
        for week in weeks:
            site = (week.source.config or {}).get("site")
            # A kitchen whose whole day is HIDDEN has published nothing, so it
            # contributes no item and no summary rather than an empty one.
            lines = [
                d.text
                for d in week.dishes
                if d.serve_date == served_on and tag_of(site, d.text) != HIDDEN
            ]
            if lines:
                day.append((week.source, week, lines))
        if not day:
            continue
        published = _at(served_on, publish_hour)
        items = [
            FiledItem(
                key=f"Matsedel/{source.config.get('site') or source.id}/{served_on}",
                url=source.config.get("url") or "",
                title=f"{source.name}, {served_on}",
                text="\n".join(lines),
                published_at=published,
            )
            for source, _week, lines in day
        ]
        post = await file_post(
            session,
            # The first restaurant's source carries the post's byline, and every
            # one of them has `type_name="matsedel"`, so which one it is only
            # decides the source name printed beside the chip.
            source=day[0][0],
            items=items,
            title=_title(served_on),
            summary=" · ".join(summary_line(s, lines) for s, _w, lines in day),
            # The day's own anchor on the week's page (0047: a filed post's
            # content lives on the correspondent's page, not at /post/{id}). The
            # `day-` prefix is what makes `#day-2026-08-24` a valid CSS selector,
            # which is what htmx resolves a boosted hash with.
            href=day_href(served_on),
            publish_at=published,
            expires_at=_at(served_on, expire_hour),
        )
        posts.append(post)
    log.info("Filed %d lunch post(s) for the week of %s", len(posts), monday)
    return posts
