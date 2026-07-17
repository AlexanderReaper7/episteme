"""Polite HTTP for all source traffic.

Every adapter fetches through `polite_get()`: it enforces a single global
throttle across all outbound requests (min-gap clamp over a normal distribution),
so no matter how many sources or items are queued, upstream servers see slow,
human-ish request pacing.

Two transport *modes*, chosen per source (`http_mode` in the Source config):

- ``polite`` (default): an honest httpx client identifying as `http_user_agent`.
  Correct and truthful; the right choice for any source that accepts it.
- ``impersonate``: curl_cffi presenting a real browser's TLS/JA3 fingerprint
  (`impersonate_profile`). For publishers whose bot-detector fingerprints the TLS
  handshake and rejects honest clients regardless of User-Agent, even for feeds
  their robots.txt permits (Phys.org is the worked example: every httpx request
  429s, curl_cffi with a Chrome fingerprint gets the feed). It changes only how
  we *look*, never how hard we hit — the throttle, conditional GETs and 429
  cooldowns all still apply. Opt a source in only after checking robots.txt
  permits the path, and never use it to reach anything robots.txt disallows.

Escalation (policy, applied in worker.tasks): a source blocked in ``polite`` mode
is bumped to ``impersonate`` and retried once, then the working mode is persisted.
"""

import asyncio
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
from curl_cffi import requests as cffi

from ..config import settings

HttpMode = Literal["polite", "impersonate"]
DEFAULT_MODE: HttpMode = "polite"
# Ordered from most honest to most aggressive; escalate_mode walks this forward.
MODE_LADDER: tuple[HttpMode, ...] = ("polite", "impersonate")

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


class FetchError(Exception):
    """Transport-agnostic HTTP error (>= 400), so callers need not know which
    client produced it. Carries what the retry/cooldown logic needs."""

    def __init__(self, status_code: int, headers: httpx.Headers) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.headers = headers


class FetchResponse:
    """Minimal, transport-agnostic response returned by `polite_get`."""

    def __init__(
        self, status_code: int, content: bytes, text: str, headers: httpx.Headers
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = headers  # case-insensitive (httpx.Headers)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise FetchError(self.status_code, self.headers)


def normalize_mode(value: str | None) -> HttpMode:
    return "impersonate" if value == "impersonate" else DEFAULT_MODE


def escalate_mode(mode: str | None) -> HttpMode | None:
    """Next stronger mode after `mode`, or None if already at the top."""
    current = normalize_mode(mode)
    i = MODE_LADDER.index(current)
    return MODE_LADDER[i + 1] if i + 1 < len(MODE_LADDER) else None


async def polite_get(
    url: str, *, mode: HttpMode = DEFAULT_MODE, extra_headers: dict[str, str] | None = None
) -> FetchResponse:
    """Throttled GET in the given transport mode. Redirects are followed."""
    await polite_wait()
    if mode == "impersonate":
        # curl_cffi supplies a full browser header set (UA, sec-ch-ua, Accept, ...)
        # matching the impersonated profile; we add only conditional-GET headers.
        # Runs in a thread: curl_cffi's requests API is sync (its async path uses
        # its own loop, which conflicts with the caller's).
        # curl_cffi types `impersonate` as a Literal of known profiles; ours comes
        # from config as a plain str, so hand it over as Any.
        profile: Any = settings.impersonate_profile

        def _blocking_get() -> cffi.Response:
            return cffi.get(
                url,
                impersonate=profile,
                headers=extra_headers or None,
                timeout=settings.http_timeout_seconds,
                allow_redirects=True,
            )

        response = await asyncio.to_thread(_blocking_get)
        resp_headers = {k: v for k, v in response.headers.items() if v is not None}
        return FetchResponse(
            response.status_code,
            response.content,
            response.text,
            httpx.Headers(resp_headers),
        )

    headers = {"User-Agent": settings.http_user_agent}
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, follow_redirects=True, headers=headers
    ) as client:
        response = await client.get(url)
    return FetchResponse(
        response.status_code, response.content, response.text, response.headers
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
