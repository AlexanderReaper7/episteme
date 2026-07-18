import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from ..config import settings
from ..db import SessionLocal
from ..ingest import content_hash, get_adapter
from ..ingest.base import RawItem
from ..ingest.http import FetchError, escalate_mode, parse_retry_after
from ..models import Source, SourceItem
from .app import app

log = logging.getLogger("episteme.worker")

BLOCKED_STATUSES = (403, 429)


async def _fetch_with_escalation(adapter, source: Source) -> list[RawItem]:
    """Fetch a source, escalating its HTTP mode past a block once (policy: a
    source that fails politely gets stronger, still-non-destructive means before
    we cool it down). Persists the working mode onto the source so the next run
    starts there. Raises FetchError if even the escalated attempt is blocked."""
    try:
        return await adapter.fetch(source, source.last_fetched_at)
    except FetchError as exc:
        stronger = escalate_mode(source.config.get("http_mode"))
        if exc.status_code not in BLOCKED_STATUSES or not settings.http_escalate_on_block:
            raise
        if stronger is None:
            raise  # already at the strongest mode
        log.warning(
            "Source %r blocked (%d); escalating http_mode to %r and retrying",
            source.name, exc.status_code, stronger,
        )
        source.config = {**source.config, "http_mode": stronger}
        return await adapter.fetch(source, source.last_fetched_at)


@app.task(name="episteme.ingest_source", retry=2)
async def ingest_source(source_id: int) -> None:
    """Fetch one source, extract full text for new items, store them."""
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        if source is None or not source.enabled:
            return
        now = datetime.now(UTC)
        if source.cooldown_until is not None and source.cooldown_until > now:
            log.info(
                "Source %r cooling down until %s; skipping", source.name, source.cooldown_until
            )
            return

        adapter = get_adapter(source.type_name)
        try:
            raw_items = await _fetch_with_escalation(adapter, source)
        except FetchError as exc:
            if exc.status_code == 429:
                # Rate-limited: retrying now makes it worse. Honor Retry-After
                # (fall back to a long default) and let a later run pick it up.
                # Any http_mode escalation is committed alongside the cooldown, so
                # the next attempt starts in the stronger mode.
                seconds = (
                    parse_retry_after(exc.headers.get("Retry-After"))
                    or settings.rate_limit_cooldown_seconds
                )
                source.cooldown_until = now + timedelta(seconds=seconds)
                await session.commit()
                log.warning(
                    "Source %r rate-limited (429); cooling down for %ds", source.name, seconds
                )
                return
            raise

        # Skip items we already have before paying for full-text extraction.
        hashes = {content_hash(item.url): item for item in raw_items}
        existing = set(
            (
                await session.execute(
                    select(SourceItem.hash).where(SourceItem.hash.in_(hashes))
                )
            ).scalars()
        )

        # No pacing here: the global throttle in ingest.http spaces out every
        # outbound request, including each extraction fetch below.
        stored = 0
        for item_hash, raw in hashes.items():
            if item_hash in existing:
                continue
            fetch_status = "extracted"
            extracted_text = None
            media_refs = list(raw.media_refs)
            try:
                extracted = await adapter.extract(raw, source)
                extracted_text = extracted.text
                seen_urls = {ref.get("url") for ref in media_refs}
                media_refs.extend(
                    ref for ref in extracted.media_refs if ref.get("url") not in seen_urls
                )
                if not extracted_text:
                    fetch_status = "extract_empty"
            except Exception as exc:
                fetch_status = "extract_failed"
                log.warning("Extraction failed for %s: %s", raw.url, exc)

            # ON CONFLICT guards against a concurrent run of the same source.
            await session.execute(
                insert(SourceItem)
                .values(
                    source_id=source.id,
                    url=raw.url,
                    hash=item_hash,
                    title=raw.title,
                    author=raw.author,
                    published_at=raw.published_at,
                    raw_content=raw.summary,
                    extracted_text=extracted_text,
                    media_refs=media_refs,
                    fetch_status=fetch_status,
                )
                .on_conflict_do_nothing(index_elements=["hash"])
            )
            stored += 1

        source.last_fetched_at = datetime.now(UTC)
        await session.commit()
        log.info("Source %r: %d fetched, %d new", source.name, len(raw_items), stored)


def _due_for_scheduled_fetch(source: Source, now: datetime) -> bool:
    """Per-source poll interval (`fetch_interval_minutes` config): a source with a
    touchy rate limiter can be polled less often than the global ingest cron. Only
    the scheduled path honors it — a manual defer of ingest_source still forces."""
    try:
        interval = float(source.config.get("fetch_interval_minutes", 0))
    except (TypeError, ValueError):
        return True
    if interval <= 0 or source.last_fetched_at is None:
        return True
    return source.last_fetched_at + timedelta(minutes=interval) <= now


@app.task(name="episteme.ingest_all")
async def ingest_all() -> None:
    """Defer an ingest_source job for every enabled, non-cooling-down source
    that is due per its own fetch interval."""
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        sources = (
            (
                await session.execute(
                    select(Source).where(
                        Source.enabled,
                        or_(
                            Source.cooldown_until.is_(None),
                            Source.cooldown_until <= now,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        source_ids = [s.id for s in sources if _due_for_scheduled_fetch(s, now)]
    for source_id in source_ids:
        await ingest_source.defer_async(source_id=source_id)
    log.info("Deferred ingestion for %d sources", len(source_ids))


@app.periodic(cron=settings.ingest_cron)
@app.task(name="episteme.scheduled_ingest")
async def scheduled_ingest(timestamp: int) -> None:
    await ingest_all.defer_async()
