"""Matsedel's one job: read every kitchen, store the week, file its posts.

**Once a weekday, and almost always for nothing.** This used to run once a week,
Monday 04:00 local, on the argument that a menu is a weekly document and seven
reads of it are six wasted. What that argument missed is that the document does
not exist yet at 04:00 on Monday: on 2026-08-31 Vanerparken was still showing
week 35 and Kalasboden was printing `Meny for denna dag saknas.` for all five
days, and there was no second read until the following Monday, so week 36 went
the whole week with two of four kitchens empty (0054).

The requests did not go up sevenfold. `store.kitchens_with_a_full_week` skips a
kitchen whose five weekdays are already stored, so an ordinary week costs one
read of each site on Monday and nothing at all for the rest of it. Only a kitchen
that has not published is fetched again, which is the only kitchen there is
anything to learn about. Politeness is a hard requirement (0005), and this spends
strictly fewer requests than a schedule that re-read all four every day would.

**One kitchen failing does not lose the others.** A site that has been redesigned
raises `MatsedelError`, which is recorded and stepped over. A site that is merely
a week behind raises `NotPublishedYet` and is not a failure at all - it is the
expected state of a Monday morning, and calling it one would put an alarm in the
job history every week. The job fails only when nothing could be read and nothing
was already complete, which is the case worth waking up to.

The five weekday posts still come from whichever read finally lands, each with
its own `publish_at`, which is what keeps lunch independent of the pipeline's
health: no LLM runs here, and a site being down on Wednesday costs nothing
because Wednesday's post was minted the moment the week was readable.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from ...config import settings
from ...db import SessionLocal
from ...worker.app import app
from .posts import file_week
from .readers import MatsedelError, NotPublishedYet, read_source
from .sources import ensure_sources, restaurants
from .store import kitchens_with_a_full_week, store_week

log = logging.getLogger("episteme.correspondents.matsedel")


@app.task(name="episteme.matsedel_scrape", retry=1)
async def matsedel_scrape() -> None:
    """Read every kitchen still missing part of this week, store it, file the posts."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    read: list[str] = []
    behind: list[str] = []
    failed: list[str] = []
    mondays: set[date] = set()
    async with SessionLocal() as session:
        await ensure_sources(session)
        await session.commit()
        whole = await kitchens_with_a_full_week(session, monday)
        for source in await restaurants(session):
            if source.id in whole:
                continue
            try:
                # The week is passed in rather than inferred from the page,
                # because a page showing last week parses perfectly.
                menu = await read_source(source, today=today, wanted_monday=monday)
            except NotPublishedYet as exc:
                behind.append(source.name)
                log.info("%s", exc)
                continue
            except MatsedelError as exc:
                failed.append(f"{source.name}: {exc}")
                log.warning("Could not read %s: %s", source.name, exc)
                continue
            await store_week(session, source, menu)
            mondays.add(menu.monday)
            read.append(source.name)
        await session.commit()

        if failed and not read and not whole:
            raise RuntimeError(
                "No kitchen could be read: " + "; ".join(failed)
            )
        for filed in sorted(mondays):
            await file_week(session, filed)
        await session.commit()
    log.info(
        "Matsedel week of %s: %d read (%s), %d already whole, %d not published yet "
        "(%s), %d failed",
        monday,
        len(read), ", ".join(read) or "-",
        len(whole),
        len(behind), ", ".join(behind) or "-",
        len(failed),
    )


@app.periodic(cron=settings.matsedel_cron)
@app.task(name="episteme.matsedel_scheduled_scrape")
async def matsedel_scheduled_scrape(timestamp: int) -> None:
    await matsedel_scrape.defer_async()
