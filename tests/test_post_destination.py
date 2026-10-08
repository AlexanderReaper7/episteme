"""One destination rule, checked over every kind there is.

`_feed.html` used to hardcode `/post/{id}` in both card branches, so reaching an
aggregated article took two clicks through a page whose only content was a
relisting of the cluster the card had already shown. The rule that replaces it
(0047) is that a post stores where its content lives: NULL `href` means it
renders itself, anything else is where both the card and `/post/{id}` go.

The parametrization is over `models.POST_KINDS`, not over the two names that
happen to be in it today, for the same reason `test_chat_tools.py` iterates the
tool table: a test that names `aggregate` keeps passing and stops covering
anything the moment a third kind arrives. A kind added to `POST_KINDS` without a
fixture here fails `test_every_kind_has_a_fixture`, and a kind that never reaches
`POST_KINDS` cannot be assigned to `Post.kind` at all.
"""

import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from episteme.models import POST_KINDS, Post, primary_item_key
from episteme.web import app as web_app
from episteme.web.templating import _format_dt, templates

AGGREGATE_HREF = "https://example.org/first"
FILED_HREF = "/c/matsedel"


def _item(**kw):
    base = dict(
        id=1,
        title="Item title",
        url=AGGREGATE_HREF,
        # `type_name` IS the correspondent slug for a filed item (0046); it is
        # what the card walks to find whose byline to print.
        source=SimpleNamespace(name="Example", type_name="matsedel"),
        published_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
        extracted_text="body text",
        raw_content=None,
        media_refs=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _card(**kw):
    """A feed card's worth of post, whatever the kind."""
    story = SimpleNamespace(
        topics=["astro"], last_item_at=datetime(2026, 7, 21, 8, 0), items=[_item()]
    )
    base = dict(
        id=200,
        kind="article",
        story=story,
        href=None,
        sections=[],
        title="A title",
        summary="A summary",
        topics=["astro"],
        difficulty="medium",
        reading_time_minutes=4,
        banner_url=None,
        generated_at=datetime(2026, 7, 21, 8, 0),
        publish_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _render(post, labels=None) -> str:
    """One card, through the real template and the real context keys."""
    return templates.env.get_template("_feed.html").render(
        posts=[post],
        correspondent_labels=labels if labels is not None else {},
        feedback_contexts={
            post.id: {
                "post_id": post.id,
                "signals": {},
                "topic_signals": {},
                "source_signals": {},
                "post_topics": [],
                "post_sources": [],
                "variant": "card",
            }
        },
    )


# One builder per kind, and the builder is where the decision lives: a new kind
# has to say here whether it carries a body or an href, which is the same
# decision `POST_KINDS` records.
BUILDERS = {
    "article": lambda: _card(kind="article", sections=[{"type": "prose", "text": "x"}]),
    "aggregate": lambda: _card(kind="aggregate", href=AGGREGATE_HREF, sections=[]),
    # A filed post's destination is its correspondent's own page, which is
    # same-origin - the one href that is not an outbound link.
    "filed": lambda: _card(kind="filed", href=FILED_HREF, sections=[]),
}

KINDS = sorted(POST_KINDS)


def test_every_kind_has_a_fixture():
    """The guard that keeps the parametrized tests below from going vacuous."""
    assert sorted(BUILDERS) == KINDS


@pytest.mark.parametrize("kind", KINDS)
def test_a_post_carries_a_body_exactly_when_it_renders_itself(kind):
    """The Python-side reading of `ck_posts_body_iff_self_rendering`. The database
    is what enforces it; this is what stops a fixture, and by extension the kind's
    producer, from being written against a shape the database would reject."""
    post = BUILDERS[kind]()
    assert (post.href is None) == bool(post.sections)
    assert (post.href is None) is POST_KINDS[kind].renders_itself


@pytest.mark.parametrize("kind", KINDS)
def test_the_card_links_where_the_post_says(kind):
    post = BUILDERS[kind]()
    html = _render(post)
    links = re.findall(r'<a class="card-link"[^>]*href="([^"]+)"', html)
    assert links == [post.href or f"/post/{post.id}"]
    # The card leaves, the provenance link does not follow it out.
    assert f'href="/post/{post.id}/provenance"' in html


def test_an_unregistered_kind_cannot_be_assigned():
    """What makes `POST_KINDS` a registry rather than a comment. Without this a
    tenth kind reaches the database with nobody having said where it points."""
    with pytest.raises(ValueError, match="unknown post kind"):
        Post(kind="lunch")


def test_the_primary_item_key_orders_by_publish_date_then_id():
    early = _item(id=9, published_at=datetime(2026, 7, 1, tzinfo=UTC))
    late = _item(id=2, published_at=datetime(2026, 7, 2, tzinfo=UTC))
    assert primary_item_key(early) < primary_item_key(late)
    # Same instant: the lower id leads, so the ordering is total.
    tie = _item(id=3, published_at=early.published_at)
    assert primary_item_key(tie) < primary_item_key(_item(id=4, published_at=early.published_at))
    # No publish date sorts last, matching `NULLS LAST` in PRIMARY_ITEM_ORDER.
    assert primary_item_key(late) < primary_item_key(_item(id=1, published_at=None))


# --- /post/{id} -----------------------------------------------------------
# The card and the canonical URL have to agree, so the redirect is the same rule
# read from the other end: whatever the card would have linked to.


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request(
        {"type": "http", "method": "GET", "path": "/post/200", "query_string": b"", "headers": raw}
    )


def _session_returning(post):
    class _Session:
        async def get(self, _model, _pk):
            return post

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return lambda: _Session()


@pytest.mark.parametrize("kind", [k for k in KINDS if not POST_KINDS[k].renders_itself])
async def test_post_view_redirects_a_stored_href(kind, monkeypatch):
    post = BUILDERS[kind]()
    monkeypatch.setattr(web_app, "SessionLocal", _session_returning(post))
    response = await web_app.post_view(_request({}), post.id)
    assert response.status_code == 302
    assert response.headers["location"] == post.href


@pytest.mark.parametrize("kind", [k for k in KINDS if not POST_KINDS[k].renders_itself])
async def test_a_boosted_click_gets_hx_redirect_instead(kind, monkeypatch):
    """htmx turns a boosted click into a fetch, and a fetch that follows a
    cross-origin redirect dies on CORS with nothing shown. The header makes htmx
    navigate the browser instead."""
    post = BUILDERS[kind]()
    monkeypatch.setattr(web_app, "SessionLocal", _session_returning(post))
    response = await web_app.post_view(_request({"HX-Request": "true"}), post.id)
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == post.href


# --- the filed card ------------------------------------------------------
# Every claim on this card has to be one a correspondent can actually back. The
# article card's byline, reading time and difficulty are all produced by the
# writer, and a filed post has no writer, so the tests below are mostly about
# what must NOT be there.


def test_a_filed_card_names_its_correspondent_and_claims_no_author():
    post = BUILDERS["filed"]()
    html = _render(post, {"matsedel": "Matsedel"})
    assert "card-kind--filed" in html
    assert "Matsedel" in html
    # Nothing wrote it, so nothing signs it and nothing estimates how long it is.
    assert "byline" not in html
    assert "min ·" not in html
    assert post.difficulty not in html


def test_a_filed_card_falls_back_to_the_source_name():
    """A slug with no `correspondents` row is a correspondent that was removed
    while its posts were still in the feed. The card still has to say something
    true, and the outlet's own name is the truest thing left on the row."""
    post = BUILDERS["filed"]()
    html = _render(post, {})
    assert "Example" in html
    assert "Filed" not in html  # only reached when there is no item at all


def test_a_filed_card_dates_itself_by_when_it_became_visible():
    """One read of a week mints five posts in one transaction, so `generated_at`
    is the same instant on all five. `publish_at` is the only one of the two that
    says which day the card is about."""
    post = BUILDERS["filed"]()
    post.publish_at = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
    html = _render(post, {"matsedel": "Matsedel"})
    assert _format_dt(post.publish_at) in html
    assert _format_dt(post.generated_at) not in html


def test_renaming_a_correspondent_moves_the_feed_validator():
    """The label is not on the post row, so nothing else in the signature can see
    it change. Without this line a rename answers the next navigation with a 304
    and the old name stays on screen."""
    post = BUILDERS["filed"]()
    before = web_app._feed_cards_sig([post], {"matsedel": "Matsedel"})
    after = web_app._feed_cards_sig([post], {"matsedel": "Lunch"})
    assert before != after


def test_an_extra_item_moves_the_filed_validator():
    """The card counts the items ("+2 more"), and a correction that adds one does
    not touch a single column on the post row."""
    post = BUILDERS["filed"]()
    before = web_app._feed_cards_sig([post])
    post.story.items = [_item(), _item(id=2, url="https://example.org/second")]
    assert web_app._feed_cards_sig([post]) != before
