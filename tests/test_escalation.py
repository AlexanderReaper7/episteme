"""Auto-escalation policy: a source blocked in `polite` mode is bumped to
`impersonate` and retried once, and the working mode is persisted onto it."""

import httpx
import pytest

from episteme.ingest.base import RawItem
from episteme.ingest.http import FetchError
from episteme.models import Source
from episteme.worker.tasks import _fetch_with_escalation


class FakeAdapter:
    """Records the http_mode present on each fetch attempt and 429s until the
    source reaches `succeed_mode`."""

    def __init__(self, succeed_mode: str | None) -> None:
        self.succeed_mode = succeed_mode
        self.modes_seen: list[str] = []

    async def fetch(self, source: Source, since) -> list[RawItem]:
        mode = source.config.get("http_mode", "polite")
        self.modes_seen.append(mode)
        if self.succeed_mode is None or mode != self.succeed_mode:
            raise FetchError(429, httpx.Headers())
        return [RawItem(url="https://example.org/a")]


def _source() -> Source:
    return Source(type_name="rss", name="S", config={"feed_url": "https://example.org/feed"})


async def test_escalates_to_impersonate_and_persists():
    adapter = FakeAdapter(succeed_mode="impersonate")
    source = _source()

    items = await _fetch_with_escalation(adapter, source)

    assert len(items) == 1
    assert adapter.modes_seen == ["polite", "impersonate"]  # tried honest first
    assert source.config["http_mode"] == "impersonate"  # persisted for next run


async def test_no_escalation_when_polite_succeeds():
    adapter = FakeAdapter(succeed_mode="polite")
    source = _source()

    await _fetch_with_escalation(adapter, source)

    assert adapter.modes_seen == ["polite"]
    assert "http_mode" not in source.config  # stays honest, untouched


async def test_reraises_when_strongest_mode_still_blocked():
    adapter = FakeAdapter(succeed_mode=None)  # blocks in every mode
    source = _source()
    source.config = {**source.config, "http_mode": "impersonate"}

    with pytest.raises(FetchError):
        await _fetch_with_escalation(adapter, source)
    assert adapter.modes_seen == ["impersonate"]  # no pointless re-tries past the top


async def test_non_block_error_is_not_escalated():
    """A 500 is the server's problem, not our fingerprint — don't escalate."""

    class ServerErrorAdapter:
        modes_seen: list[str] = []

        async def fetch(self, source, since):
            self.modes_seen.append(source.config.get("http_mode", "polite"))
            raise FetchError(500, httpx.Headers())

    adapter = ServerErrorAdapter()
    with pytest.raises(FetchError):
        await _fetch_with_escalation(adapter, _source())
    assert adapter.modes_seen == ["polite"]  # tried once, no escalation
