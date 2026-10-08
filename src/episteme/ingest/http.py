"""Polite HTTP for all source traffic.

Every adapter fetches through `polite_get()`: it enforces a single global
throttle across all outbound requests (min-gap clamp over a normal distribution),
so no matter how many sources or items are queued, upstream servers see slow,
human-ish request pacing.

Two transport *modes*, chosen per source (`http_mode` in the Source config):

- ``polite`` (default): an honest httpx client identifying as `user_agent()`.
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
from urllib.parse import urlparse

import httpx
from curl_cffi import requests as cffi

from ..config import settings

HttpMode = Literal["polite", "impersonate"]
DEFAULT_MODE: HttpMode = "polite"
# Ordered from most honest to most aggressive; escalate_mode walks this forward.
MODE_LADDER: tuple[HttpMode, ...] = ("polite", "impersonate")

_lock = asyncio.Lock()
_last_request_at: float = 0.0
_host_last_request_at: dict[str, float] = {}


async def polite_wait(host: str | None = None, host_gap_seconds: float = 0.0) -> None:
    """Block until the globally-throttled next request slot. When a host and a
    per-host gap are given, additionally wait until that many seconds have passed
    since the last request to the same host — the lever for sources whose rate
    limiter trips at the global ~3s pacing (per-source `min_request_gap_seconds`).
    The per-host wait sleeps outside the lock so it never stalls other hosts."""
    global _last_request_at
    while True:
        async with _lock:
            gap = max(
                settings.polite_delay_min_seconds,
                random.gauss(
                    settings.polite_delay_mean_seconds, settings.polite_delay_stddev_seconds
                ),
            )
            now = time.monotonic()
            ready = _last_request_at + gap
            if host and host_gap_seconds > 0:
                ready = max(
                    ready, _host_last_request_at.get(host, float("-inf")) + host_gap_seconds
                )
            if ready <= now:
                _last_request_at = now
                if host:
                    _host_last_request_at[host] = now
                return
            wait = ready - now
        await asyncio.sleep(wait)


class FetchError(Exception):
    """Transport-agnostic HTTP error (>= 400), so callers need not know which
    client produced it. Carries what the retry/cooldown logic needs."""

    def __init__(self, status_code: int, headers: httpx.Headers) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.headers = headers


class FetchResponse:
    """Minimal, transport-agnostic response returned by `polite_get`."""

    def __init__(self, status_code: int, content: bytes, text: str, headers: httpx.Headers) -> None:
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


def user_agent() -> str:
    """`http_user_agent`, with `+mailto:<http_contact>` when a contact is set."""
    if settings.http_contact:
        return f"{settings.http_user_agent} +mailto:{settings.http_contact}"
    return settings.http_user_agent


async def polite_get(
    url: str,
    *,
    mode: HttpMode = DEFAULT_MODE,
    extra_headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
    extensions: dict | None = None,
    host_gap_seconds: float = 0.0,
) -> FetchResponse:
    """Throttled GET in the given transport mode. Redirects are followed unless
    `follow_redirects=False` (the research fetcher disables auto-follow so it can
    re-run its SSRF check on each hop — see research.tools). `extensions` are httpx
    request extensions (e.g. `sni_hostname` for the research fetcher's DNS pinning);
    honored only in ``polite`` mode — curl_cffi has no equivalent. A positive
    `host_gap_seconds` adds per-host spacing on top of the global throttle (see
    polite_wait) — adapters pass the source's `min_request_gap_seconds` config."""
    await polite_wait(urlparse(url).hostname, host_gap_seconds)
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
                allow_redirects=follow_redirects,
            )

        response = await asyncio.to_thread(_blocking_get)
        resp_headers = {k: v for k, v in response.headers.items() if v is not None}
        return FetchResponse(
            response.status_code,
            response.content,
            response.text,
            httpx.Headers(resp_headers),
        )

    headers = {"User-Agent": user_agent()}
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, follow_redirects=follow_redirects, headers=headers
    ) as client:
        response = await client.get(url, extensions=extensions)
    return FetchResponse(response.status_code, response.content, response.text, response.headers)


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
