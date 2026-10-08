"""The morning digest: what last night's run produced, pushed to the phone (0056).

A cron of its own rather than a hook on the end of `run_pipeline`, because the
run finishes when it finishes. It starts at 03:00 and the length of it depends on
how many stories triage passed, so hanging the notification off its last commit
means a phone that buzzes at 04:12 on a light night and 06:40 on a heavy one. The
digest is read over breakfast, so it is sent at breakfast, and the run row is
still there to be read whenever that is.

**What counts as new is `summarized_at`, not `generated_at`.** A card is what the
reader gets, and a post is not in the feed until it has one (0050), so the column
that gates visibility is the column that answers "what appeared overnight". It
also keeps the lunch posts out: a filed post brings its own summary and is never
stamped, so today's menu is counted by the notification that exists for it and
not a second time here.

The digest reports whatever it finds, including a run that never happened. That
is not a failure alarm bolted on (the reader did not ask for one); it is the
digest declining to imply that silence means a quiet night.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from .. import notify
from ..config import settings
from ..db import SessionLocal
from ..models import PipelineRun, Post
from .app import app

log = logging.getLogger("episteme.worker.digest")

MAX_HEADLINES = 5
# One card's title on a phone's lock screen. Longer ones are cut rather than
# wrapped into a wall of text nobody reads past.
MAX_TITLE_CHARS = 90


async def latest_finished_run(session) -> PipelineRun | None:
    return (
        (
            await session.execute(
                select(PipelineRun)
                .where(PipelineRun.finished_at.is_not(None))
                .order_by(PipelineRun.started_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def cards_since(session, since: datetime) -> list[Post]:
    """Published posts whose card was written on or after `since`, best first.

    Ordered by `affinity_score`, which the `score` stage writes as the last thing
    the run does, so by the time this reads them the ranking is the same one the
    feed will use (minus freshness, which is applied in the query, 0020).
    """
    return list(
        (
            await session.execute(
                select(Post)
                .where(
                    Post.status == "published",
                    Post.summarized_at.is_not(None),
                    Post.summarized_at >= since,
                )
                .order_by(Post.affinity_score.desc().nullslast(), Post.id.desc())
            )
        )
        .scalars()
        .all()
    )


def _stage_line(run: PipelineRun) -> str:
    """`triage 28 · write 6 · summarize 12`, skipping the stages that did nothing.

    Zero is the normal state for most stages on most nights, and printing all
    eight of them buries the two that moved.
    """
    stages = run.stages or {}
    moved = [f"{name} {count}" for name, count in stages.items() if count]
    return " · ".join(moved)


def compose(
    run: PipelineRun | None, cards: list[Post], now: datetime
) -> tuple[str, str, tuple[str, ...], int]:
    """(title, message, tags, priority) for the run, whatever state it is in.

    Pure, so the wording is testable without a database or a network.
    """
    if run is None or run.finished_at is None:
        return (
            "Episteme: no run yet",
            "No pipeline run has finished. Nothing has been written.",
            ("hourglass",),
            notify.DEFAULT,
        )
    age = now - run.finished_at
    if age > timedelta(hours=settings.digest_stale_hours):
        hours = int(age.total_seconds() // 3600)
        return (
            "Episteme: no run last night",
            f"The last run finished {hours}h ago and ended {run.status}."
            + (f"\n{run.error}" if run.error else ""),
            ("warning",),
            notify.HIGH,
        )
    if run.status != "succeeded":
        # skipped is llama-server having been unreachable, which is a night off
        # rather than a break (0055); failed and paused are their own things. All
        # three mean the same to a reader: the feed did not grow.
        return (
            f"Episteme: run {run.status}",
            (run.error or "No detail recorded.") + f"\n{_stage_line(run)}".rstrip(),
            ("warning",),
            notify.HIGH,
        )
    if not cards:
        return (
            "Episteme: nothing new",
            f"The run succeeded and added no cards.\n{_stage_line(run)}".rstrip(),
            ("sleeping",),
            notify.LOW,
        )
    headlines = "\n".join(
        f"• {(post.title or 'Untitled')[:MAX_TITLE_CHARS]}" for post in cards[:MAX_HEADLINES]
    )
    more = len(cards) - MAX_HEADLINES
    if more > 0:
        headlines += f"\n… and {more} more"
    plural = "" if len(cards) == 1 else "s"
    return (
        f"Episteme: {len(cards)} new card{plural}",
        f"{headlines}\n\n{_stage_line(run)}".rstrip(),
        ("newspaper",),
        notify.DEFAULT,
    )


@app.task(name="episteme.send_digest")
async def send_digest() -> None:
    """Read the newest finished run and push its digest."""
    if not notify.enabled():
        log.info("No ntfy configured; skipping the digest")
        return
    async with SessionLocal() as session:
        run = await latest_finished_run(session)
        cards = await cards_since(session, run.started_at) if run is not None else []
        title, message, tags, priority = compose(run, cards, datetime.now(UTC))
    await notify.publish(
        settings.ntfy_topic_digest,
        title,
        message,
        tags=tags,
        priority=priority,
        click=notify.link("/"),
    )


@app.periodic(cron=settings.digest_cron)
@app.task(name="episteme.scheduled_digest")
async def scheduled_digest(timestamp: int) -> None:
    await send_digest.defer_async()
