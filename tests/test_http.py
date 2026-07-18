import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

import episteme.ingest.http as polite_http
from episteme.config import settings
from episteme.ingest.http import (
    FetchError,
    FetchResponse,
    escalate_mode,
    normalize_mode,
    parse_retry_after,
    polite_wait,
)


def test_parse_retry_after_seconds():
    assert parse_retry_after("120") == 120


def test_parse_retry_after_http_date():
    future = datetime.now(UTC) + timedelta(seconds=300)
    result = parse_retry_after(format_datetime(future, usegmt=True))
    assert result is not None
    assert 290 <= result <= 300


def test_parse_retry_after_past_date_clamps_to_zero():
    past = datetime.now(UTC) - timedelta(seconds=300)
    assert parse_retry_after(format_datetime(past, usegmt=True)) == 0


def test_parse_retry_after_invalid():
    assert parse_retry_after(None) is None
    assert parse_retry_after("soonish") is None


def test_normalize_mode_defaults_to_polite():
    """Unknown/absent modes fall back to the honest transport, never impersonate."""
    assert normalize_mode(None) == "polite"
    assert normalize_mode("") == "polite"
    assert normalize_mode("nonsense") == "polite"
    assert normalize_mode("impersonate") == "impersonate"


def test_escalate_mode_walks_the_ladder_then_stops():
    assert escalate_mode("polite") == "impersonate"
    assert escalate_mode(None) == "impersonate"  # unknown normalizes to polite first
    assert escalate_mode("impersonate") is None  # already strongest


async def test_polite_wait_enforces_per_host_gap(monkeypatch):
    """The Phys.org lever: a source with `min_request_gap_seconds` gets extra
    spacing between requests to ITS host, without slowing other hosts."""
    monkeypatch.setattr(settings, "polite_delay_min_seconds", 0.0)
    monkeypatch.setattr(settings, "polite_delay_mean_seconds", 0.0)
    monkeypatch.setattr(settings, "polite_delay_stddev_seconds", 0.0)
    monkeypatch.setattr(polite_http, "_last_request_at", 0.0)
    monkeypatch.setattr(polite_http, "_host_last_request_at", {})

    start = time.monotonic()
    await polite_wait("slow.example", 0.3)  # first request to the host: no wait
    await polite_wait("other.example", 0.3)  # different host: unaffected
    await polite_wait(None, 0.0)  # no host key (research/search): unaffected
    elapsed_others = time.monotonic() - start
    await polite_wait("slow.example", 0.3)  # same host again: waits out the gap
    elapsed_same = time.monotonic() - start

    assert elapsed_others < 0.15
    assert elapsed_same >= 0.28


def test_fetch_response_raises_only_on_error_status():
    ok = FetchResponse(200, b"body", "body", httpx.Headers())
    ok.raise_for_status()  # no raise

    not_modified = FetchResponse(304, b"", "", httpx.Headers())
    not_modified.raise_for_status()  # 304 is not an error

    with pytest.raises(FetchError) as exc:
        FetchResponse(429, b"", "", httpx.Headers({"Retry-After": "60"})).raise_for_status()
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"
