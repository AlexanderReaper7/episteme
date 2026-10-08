"""Notifications: the payload on the wire, and the words in it (0056).

A push has no user watching it fail. `publish` swallows every error by design, so
the only thing standing between "the phone buzzed" and "the log has one warning
nobody reads" is a test that asserts the bytes. That is what the `transport` seam
is for, and it is why these tests check the request rather than a mock's call
arguments.

The wording lives in `digest.compose`, which is pure for exactly this reason: five
states of a pipeline run, each with a different thing to say, none of them worth a
database to check.
"""

import json
import re
import struct
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from episteme import notify
from episteme.config import settings
from episteme.correspondents.matsedel.posts import day_href
from episteme.correspondents.matsedel.tasks import lunch_message
from episteme.models import PipelineRun, Post
from episteme.web.templating import BASE_DIR
from episteme.worker import digest

STATIC = BASE_DIR / "static"
MANIFEST = STATIC / "manifest.webmanifest"
MATSEDEL = Path(__file__).resolve().parent.parent / "src/episteme/correspondents/matsedel"


@pytest.fixture
def ntfy(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_base_url", "https://ntfy.example.ts.net")
    monkeypatch.setattr(settings, "ntfy_token", "")
    monkeypatch.setattr(settings, "public_base_url", "")


def _capture(response: httpx.Response | None = None):
    """A transport that records the requests it is given and answers 200."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response if response is not None else httpx.Response(200, text="{}")

    return seen, httpx.MockTransport(handler)


# --- what goes on the wire ---------------------------------------------------------


async def test_the_topic_is_a_field_and_the_body_is_utf8(ntfy):
    """The whole reason for the JSON format: HTTP headers cannot carry a-ring.

    `Pannbiff med pepparsas` in the header format is either mojibake on the phone
    or a 400 from the server, and both look like "notifications are broken" long
    after the change that caused it.
    """
    seen, transport = _capture()
    assert await notify.publish(
        "episteme-lunch", "Lunch on Tuesday", "Pannbiff med pepparsås", transport=transport
    )
    (request,) = seen
    assert str(request.url) == "https://ntfy.example.ts.net/"
    body = json.loads(request.content.decode("utf-8"))
    assert body["topic"] == "episteme-lunch"
    assert body["message"] == "Pannbiff med pepparsås"
    for name, value in request.headers.items():
        value.encode("ascii")  # raises UnicodeEncodeError if any menu text leaked here
        assert name != "title"


async def test_tags_and_click_are_omitted_when_absent(ntfy):
    """ntfy reads an empty `tags` as a tag list, not as no tags."""
    seen, transport = _capture()
    await notify.publish("t", "Title", "Body", transport=transport)
    body = json.loads(seen[0].content)
    assert "tags" not in body and "click" not in body
    assert body["priority"] == notify.DEFAULT


async def test_tags_and_click_ride_along_when_given(ntfy):
    seen, transport = _capture()
    await notify.publish(
        "t",
        "Title",
        "Body",
        tags=("plate_with_cutlery",),
        priority=notify.HIGH,
        click="https://episteme.example.ts.net/c/matsedel#day-2026-09-10",
        transport=transport,
    )
    body = json.loads(seen[0].content)
    assert body["tags"] == ["plate_with_cutlery"]
    assert body["click"].endswith("#day-2026-09-10")
    assert body["priority"] == 4


async def test_a_token_becomes_a_bearer_header(ntfy, monkeypatch):
    monkeypatch.setattr(settings, "ntfy_token", "tk_secret")
    seen, transport = _capture()
    await notify.publish("t", "Title", "Body", transport=transport)
    assert seen[0].headers["authorization"] == "Bearer tk_secret"


async def test_no_token_sends_no_authorization_header(ntfy):
    seen, transport = _capture()
    await notify.publish("t", "Title", "Body", transport=transport)
    assert "authorization" not in seen[0].headers


# --- and what happens when it does not land ----------------------------------------


async def test_an_empty_base_url_is_the_off_switch(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_base_url", "")
    seen, transport = _capture()
    assert notify.enabled() is False
    assert await notify.publish("t", "Title", "Body", transport=transport) is False
    assert seen == []


async def test_a_refusing_server_returns_false_and_does_not_raise(ntfy):
    """The rule the module exists to keep: a notification never fails the work.

    A lunch menu that is already filed is still filed when ntfy is down, and a
    pipeline run that produced 12 cards did produce them.
    """
    _, transport = _capture(httpx.Response(403, text="forbidden"))
    assert await notify.publish("t", "Title", "Body", transport=transport) is False


async def test_a_dead_host_returns_false_and_does_not_raise(ntfy):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    result = await notify.publish("t", "Title", "Body", transport=httpx.MockTransport(handler))
    assert result is False


# --- where a tap lands -------------------------------------------------------------


def test_link_is_none_without_a_public_base_url(ntfy):
    """`web_internal_url` is `http://web:8200`, which no phone can resolve."""
    assert notify.link("/") is None


def test_link_keeps_the_path_and_its_anchor(ntfy, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://episteme.example.ts.net/")
    assert notify.link("/c/matsedel#day-2026-09-10") == (
        "https://episteme.example.ts.net/c/matsedel#day-2026-09-10"
    )
    assert notify.link("/") == "https://episteme.example.ts.net/"


# --- the digest's five things to say -----------------------------------------------


def _run(status="succeeded", *, age_hours=6.0, stages=None, error=None) -> PipelineRun:
    now = datetime.now(UTC)
    return PipelineRun(
        started_at=now - timedelta(hours=age_hours + 1),
        finished_at=now - timedelta(hours=age_hours),
        status=status,
        stages=stages if stages is not None else {},
        error=error,
    )


def _cards(count: int) -> list[Post]:
    return [Post(title=f"Card {i}") for i in range(count)]


def test_no_run_at_all_says_so(ntfy):
    title, message, _tags, _priority = digest.compose(None, [], datetime.now(UTC))
    assert "no run yet" in title
    assert "Nothing has been written" in message


def test_a_run_older_than_the_night_is_a_warning(ntfy):
    """Silence is the failure mode this catches: no run means no cards, and no
    cards would otherwise read exactly like a quiet night."""
    run = _run(age_hours=settings.digest_stale_hours + 9)
    title, message, _tags, priority = digest.compose(run, [], datetime.now(UTC))
    assert title == "Episteme: no run last night"
    assert re.search(r"\d+h ago", message)
    assert priority == notify.HIGH


def test_a_run_that_did_not_succeed_carries_its_error(ntfy):
    run = _run("skipped", error="llama-server unreachable", stages={"embed": 3})
    title, message, _tags, priority = digest.compose(run, [], datetime.now(UTC))
    assert title == "Episteme: run skipped"
    assert "llama-server unreachable" in message
    assert "embed 3" in message
    assert priority == notify.HIGH


def test_a_quiet_night_is_low_priority(ntfy):
    title, _message, _tags, priority = digest.compose(_run(), [], datetime.now(UTC))
    assert title == "Episteme: nothing new"
    assert priority == notify.LOW


def test_cards_are_headlines_with_a_count_of_the_rest(ntfy):
    run = _run(stages={"embed": 40, "triage": 12, "write": 0, "summarize": 7})
    title, message, _tags, _priority = digest.compose(run, _cards(8), datetime.now(UTC))
    assert title == "Episteme: 8 new cards"
    assert message.count("•") == digest.MAX_HEADLINES
    assert "and 3 more" in message
    assert "Card 0" in message and "Card 7" not in message
    # The stages that did nothing are not printed; zero is most stages most nights.
    assert "embed 40 · triage 12 · summarize 7" in message
    assert "write" not in message


def test_one_card_is_not_pluralized(ntfy):
    title, _message, _tags, _priority = digest.compose(_run(), _cards(1), datetime.now(UTC))
    assert title == "Episteme: 1 new card"


def test_a_long_title_is_cut_rather_than_wrapped(ntfy):
    long_one = [Post(title="x" * 400)]
    _title, message, _tags, _priority = digest.compose(_run(), long_one, datetime.now(UTC))
    assert len(message.splitlines()[0]) <= digest.MAX_TITLE_CHARS + 2  # the bullet


def test_a_card_with_no_title_still_prints(ntfy):
    _title, message, _tags, _priority = digest.compose(
        _run(), [Post(title=None)], datetime.now(UTC)
    )
    assert "Untitled" in message


# --- lunch -------------------------------------------------------------------------


def test_the_day_anchor_is_written_in_one_place():
    """`matsedel_notify` finds today's post by this exact string.

    Two f-strings in two modules would be one rename away from a notification that
    silently never fires, and a job history that records it as having run.
    """
    assert day_href(date(2026, 8, 24)) == "/c/matsedel#day-2026-08-24"
    writers = [
        path.name
        for path in sorted(MATSEDEL.glob("*.py"))
        if "#day-" in path.read_text(encoding="utf-8")
    ]
    assert writers == ["posts.py"]


def test_the_anchor_matches_the_id_the_page_renders():
    week = (MATSEDEL / "templates" / "matsedel_week.html").read_text(encoding="utf-8")
    assert 'id="day-{{ day.date }}"' in week


def test_one_kitchen_per_line_on_a_lock_screen():
    summary = "Koppargrillen: Pannbiff med pepparsås · Kalasboden: Fisksoppa"
    assert lunch_message(summary) == (
        "Koppargrillen: Pannbiff med pepparsås\nKalasboden: Fisksoppa"
    )
    assert lunch_message(None) == ""


# --- the home screen icon ----------------------------------------------------------


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", header[16:24])


def test_the_manifest_is_linked_from_every_page():
    base = (BASE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' in base and "manifest.webmanifest" in base


def test_the_manifest_declares_a_standalone_app_at_the_root():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["display"] == "standalone"
    assert data["start_url"] == "/" and data["scope"] == "/"
    assert data["background_color"] == "#000000"  # always dark mode, true black


def test_every_declared_icon_exists_at_the_size_it_claims():
    """A manifest icon that 404s is not an error anywhere: Android installs the
    shortcut with the browser's own glyph on it."""
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for icon in data["icons"]:
        path = STATIC / icon["src"].removeprefix("/static/")
        assert path.exists(), icon["src"]
        width, height = _png_size(path)
        assert f"{width}x{height}" == icon["sizes"]


def test_a_maskable_icon_ships_alongside_a_plain_one():
    """Only one of the two is a trap either way: a launcher that crops would cut
    the obelisk's tips off a plain icon, and one that does not crop would show a
    maskable icon's padding as dead space around a shrunken mark."""
    icons = json.loads(MANIFEST.read_text(encoding="utf-8"))["icons"]
    assert {icon["purpose"] for icon in icons} == {"any", "maskable"}


def test_the_manifest_is_served_as_a_manifest():
    """Chrome ignores a manifest served as octet-stream, silently."""
    import mimetypes

    import episteme.web.app  # noqa: F401  (registering the type is an import side effect)

    assert mimetypes.guess_type("x.webmanifest")[0] == "application/manifest+json"
