"""Matsedel's one job: read every kitchen, store the week, file its posts.

**Once a week, not once a day.** The menus are weekly documents, so a daily
scrape is seven times the requests for the same bytes, and politeness is a hard
requirement rather than a preference (0005). The five weekday posts come from
that single read, each with its own `publish_at`, which is also what keeps lunch
independent of the pipeline's health: no LLM runs here, and a site being down on
Wednesday costs nothing because Wednesday's post was minted on Monday.

**One kitchen failing does not lose the others.** A site that has been redesigned
raises `MatsedelError`, which is recorded and stepped over; the job fails only if
no kitchen at all could be read, which is the case worth waking up to.
"""

from __future__ import annotations

import logging
from datetime import date

from ...config import settings
from ...db import SessionLocal
from ...worker.app import app
from .posts import file_week
from .readers import MatsedelError, read_source
from .sources import ensure_sources, restaurants
from .store import store_week

log = logging.getLogger("episteme.correspondents.matsedel")


@app.task(name="episteme.matsedel_scrape", retry=1)
async def matsedel_scrape() -> None:
    """Read every enabled kitchen, store what it published, file the week's posts."""
    today = date.today()
    read: list[str] = []
    failed: list[str] = []
    mondays: set[date] = set()
    async with SessionLocal() as session:
        await ensure_sources(session)
        await session.commit()
        for source in await restaurants(session):
            try:
                menu = await read_source(source, today=today)
            except MatsedelError as exc:
                failed.append(f"{source.name}: {exc}")
                log.warning("Could not read %s: %s", source.name, exc)
                continue
            await store_week(session, source, menu)
            mondays.add(menu.monday)
            read.append(source.name)
        await session.commit()

        if not read:
            raise RuntimeError(
                "No kitchen could be read: " + "; ".join(failed or ["no sources"])
            )
        for monday in sorted(mondays):
            await file_week(session, monday)
        await session.commit()
    log.info(
        "Matsedel: read %d kitchen(s) (%s), %d failed",
        len(read), ", ".join(read), len(failed),
    )


@app.periodic(cron=settings.matsedel_cron)
@app.task(name="episteme.matsedel_scheduled_scrape")
async def matsedel_scheduled_scrape(timestamp: int) -> None:
    await matsedel_scrape.defer_async()
