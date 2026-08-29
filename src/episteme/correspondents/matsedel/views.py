"""Matsedel's three pages: the week, its Glance block, and what it has collected.

The router carries paths relative to `/c/matsedel`, which core owns and mounts
(0046). Nothing here decides where it lands, and nothing here checks the enabled
flag: the mount carries that as a dependency, so every route added later is
covered by construction.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from ...db import SessionLocal
from ...models import Source
from ..registry import get_plugin
from ...web.templating import correspondent_page, templates
from .models import MatsedelDish, MatsedelWeek
from .store import latest_monday, stored_weeks, week_of
from .tags import DISH, HIDDEN, LABEL, tag_of

router = APIRouter()

SLUG = "matsedel"

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")



def _monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _parse_monday(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return _monday_of(date.fromisoformat(value))
    except ValueError:
        return None


async def _week_context(session, monday: date) -> dict:
    """One week, arranged the way both the page and the Glance block read it.

    `kitchens` is the week's roster and every day carries one cell per entry, in
    the same order, empty where a kitchen published nothing. That alignment is
    what lets the page name each restaurant ONCE in a sticky header instead of
    five times down the page: the header and each day's row are the same grid,
    so a cell has to hold a column open even when it has nothing to say. Glance
    wants the opposite and drops the empty ones itself.
    """
    weeks = await week_of(session, monday)
    kitchens = [
        {
            "name": week.source.name,
            "url": (week.source.config or {}).get("url"),
            # The `sources` config key, which is also the CSS modifier a
            # restaurant's own typeface is hung on.
            "site": (week.source.config or {}).get("site") or str(week.source.id),
        }
        for week in weeks
    ]
    days = []
    for offset, name in enumerate(WEEKDAY_NAMES):
        served_on = monday + timedelta(days=offset)
        cells = [
            {
                "kitchen": kitchen,
                "lines": [
                    {"text": dish.text, "label": tag == LABEL}
                    for dish in week.dishes
                    if dish.serve_date == served_on
                    # Walrus so a HIDDEN line is classified once and dropped
                    # here, rather than reaching a template that has to know
                    # about a third state.
                    and (tag := tag_of(kitchen["site"], dish.text)) != HIDDEN
                ],
            }
            for kitchen, week in zip(kitchens, weeks, strict=True)
        ]
        days.append({"name": name, "date": served_on, "cells": cells})
    return {
        "monday": monday,
        "kitchens": kitchens,
        "days": days,
        "kitchens_read": len(weeks),
    }


@router.get("", response_class=HTMLResponse)
async def week_view(request: Request, monday: str | None = None):
    """The week. This is where a lunch card's `href` lands, on the day's anchor."""
    async with SessionLocal() as session:
        wanted = _parse_monday(monday) or _monday_of(date.today())
        context = await _week_context(session, wanted)
        if not context["kitchens_read"] and monday is None:
            # Nothing for this week yet (the scrape has not run, or it is Sunday
            # and next week is not published). Show the most recent week there is
            # rather than an empty page that looks broken.
            fallback = await latest_monday(session, not_after=wanted)
            if fallback is not None:
                context = await _week_context(session, fallback)
        # An arrow only where there is a week to go to. Both are None on a
        # first run, when the only week stored is the one being looked at.
        previous = context["monday"] - timedelta(days=7)
        following = context["monday"] + timedelta(days=7)
        stored = await stored_weeks(session, (previous, following))
        context["previous"] = previous if previous in stored else None
        context["next"] = following if following in stored else None
        context["today"] = date.today()
    return correspondent_page(request, get_plugin(SLUG), "matsedel_week.html", context)


@router.get("/glance", response_class=HTMLResponse)
async def glance_block(request: Request):
    """Today's lunch, or the next weekday's once the kitchens have closed.

    A fragment, not a page: core's Glance swaps it into a block it already drew,
    so this response carries no wrapper and no stylesheet link.
    """
    async with SessionLocal() as session:
        today = date.today()
        # Saturday and Sunday look forward to Monday rather than back at Friday.
        wanted = today + timedelta(days=(7 - today.weekday()) if today.weekday() > 4 else 0)
        context = await _week_context(session, _monday_of(wanted))
        for candidate in context["days"]:
            candidate["kitchens"] = [c for c in candidate["cells"] if c["lines"]]
        day = next((d for d in context["days"] if d["date"] == wanted), None)
        if day is None or not day["kitchens"]:
            day = next((d for d in context["days"] if d["kitchens"]), None)
    return templates.TemplateResponse(
        request, "matsedel_glance.html", {"day": day, "monday": context["monday"]}
    )


@router.get("/stats", response_class=HTMLResponse)
async def stats_view(request: Request):
    """What Matsedel has collected. A correspondent's own page is where standing
    content lives, and the history of a menu is as standing as this gets."""
    async with SessionLocal() as session:
        per_source = (
            await session.execute(
                select(
                    Source.name,
                    func.count(func.distinct(MatsedelWeek.id)),
                    func.min(MatsedelWeek.monday),
                    func.max(MatsedelWeek.monday),
                    func.count(MatsedelDish.id),
                )
                .select_from(MatsedelWeek)
                .join(Source, Source.id == MatsedelWeek.source_id)
                .outerjoin(MatsedelDish, MatsedelDish.week_id == MatsedelWeek.id)
                .group_by(Source.name)
                .order_by(Source.name)
            )
        ).all()
        repeats = (
            await session.execute(
                select(
                    Source.name,
                    Source.config["site"].astext,
                    MatsedelDish.text,
                    func.count().label("times"),
                )
                .select_from(MatsedelDish)
                .join(MatsedelWeek, MatsedelWeek.id == MatsedelDish.week_id)
                .join(Source, Source.id == MatsedelWeek.source_id)
                .group_by(Source.name, Source.config["site"].astext, MatsedelDish.text)
                .having(func.count() > 1)
                .order_by(func.count().desc(), MatsedelDish.text)
            )
        ).all()
    return correspondent_page(
        request,
        get_plugin(SLUG),
        "matsedel_stats.html",
        {
            "kitchens": [
                {
                    "name": name,
                    "weeks": weeks,
                    "first": first,
                    "last": last,
                    "lines": lines,
                }
                for name, weeks, first, last, lines in per_source
            ],
            # SQL groups; `tag_of` filters. The alternative is the same rule
            # written a second time as a WHERE clause, in a dialect no test can
            # check against the Python one, and the tag table would have to be
            # written out as SQL literals as well. Grouping has already collapsed
            # a year of menus into a few hundred distinct lines by this point, so
            # what Python walks is the small end.
            "repeats": [
                {"name": name, "text": text, "times": times}
                for name, site, text, times in repeats
                if tag_of(site, text) == DISH
            ][:15],
        },
    )
