from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from episteme.ingest.http import parse_retry_after


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
