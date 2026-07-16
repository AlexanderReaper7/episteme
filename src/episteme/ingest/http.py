"""Polite HTTP for all source traffic.

Every adapter must fetch through `polite_client()`: it enforces a single global
throttle across all outbound requests (min-gap clamp over a normal distribution),
so no matter how many sources or items are queued, upstream servers see slow,
human-ish request pacing.
"""

import asyncio
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from ..config import settings

_lock = asyncio.Lock()
_last_request_at: float = 0.0


async def polite_wait() -> None:
    """Block until the globally-throttled next request slot."""
    global _last_request_at
    async with _lock:
        gap = max(
            settings.polite_delay_min_seconds,
            random.gauss(
                settings.polite_delay_mean_seconds, settings.polite_delay_stddev_seconds
            ),
        )
        wait = _last_request_at + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


def polite_client(extra_headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    async def _throttle(request: httpx.Request) -> None:
        await polite_wait()

    headers = {"User-Agent": settings.http_user_agent}
    if extra_headers:
        headers.update(extra_headers)
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=True,
        headers=headers,
        event_hooks={"request": [_throttle]},
    )


def parse_retry_after(value: str | None) -> int | None:
    """Retry-After header → seconds from now, or None if absent/unparseable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        delta = parsedate_to_datetime(value) - datetime.now(UTC)
        return max(0, int(delta.total_seconds()))
    except (ValueError, TypeError):
        return None
