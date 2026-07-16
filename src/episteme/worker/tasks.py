import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from ..config import settings
from ..db import SessionLocal
from ..ingest import content_hash, get_adapter
from ..ingest.http import parse_retry_after
from ..models import Source, SourceItem
from .app import app

log = logging.getLogger("episteme.worker")


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
            raw_items = await adapter.fetch(source, source.last_fetched_at)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                # Rate-limited: retrying now makes it worse. Honor Retry-After
                # (fall back to a long default) and let a later run pick it up.
                seconds = (
                    parse_retry_after(exc.response.headers.get("Retry-After"))
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
                extracted = await adapter.extract(raw)
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


@app.task(name="episteme.ingest_all")
async def ingest_all() -> None:
    """Defer an ingest_source job for every enabled, non-cooling-down source."""
    async with SessionLocal() as session:
        source_ids = (
            (
                await session.execute(
                    select(Source.id).where(
                        Source.enabled,
                        or_(
                            Source.cooldown_until.is_(None),
                            Source.cooldown_until <= datetime.now(UTC),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    for source_id in source_ids:
        await ingest_source.defer_async(source_id=source_id)
    log.info("Deferred ingestion for %d sources", len(source_ids))


@app.periodic(cron=settings.ingest_cron)
@app.task(name="episteme.scheduled_ingest")
async def scheduled_ingest(timestamp: int) -> None:
    await ingest_all.defer_async()
