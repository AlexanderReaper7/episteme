"""Conditional-GET validators for the feed and post pages (web.app).

The etags are pure functions of what the template renders, so they're exercised
here with lightweight stand-in objects — no DB. The invariants that matter:
a validator must change whenever the rendered output would (never a stale 304),
and must NOT change for a mutation the page doesn't show (so the 304 keeps paying
off). The sharpest case is a QA revision: it rewrites a feature's body sections,
which the POST page shows (etag must move) but the FEED card does not (etag must
hold)."""

from datetime import datetime
from types import SimpleNamespace

from episteme.web.app import _feed_etag as _feed_etag_impl
from episteme.web.app import _feed_partial_etag, _items_partial_etag, _provenance_etag
from episteme.web.app import _post_page_etag as _post_etag_impl

_VOICES = [SimpleNamespace(id="v1", label="Voice One", enabled=True)]


# Thin wrappers so the existing call sites read cleanly; `fragment` (the boosted-htmx
# representation) defaults to the full-document form and is exercised explicitly in
# the fragment-distinctness tests at the bottom.
def _feed_etag(page, fragment=False):
    return _feed_etag_impl(page, fragment)


_NO_FEEDBACK = {
    "signals": {},
    "topic_signals": {},
    "source_signals": {},
    "post_topics": [],
    "post_sources": [],
}


def _post_page_etag(post, voices=_VOICES, default_voice="v1", tts_configured=True,
                    fragment=False, feedback_ctx=None):
    return _post_etag_impl(
        post, voices, default_voice, tts_configured, fragment,
        _NO_FEEDBACK if feedback_ctx is None else feedback_ctx,
    )


def _item(**kw):
    base = dict(
        id=1,
        title="Item title",
        url="https://example.org/a",
        source=SimpleNamespace(name="Example"),
        published_at=datetime(2026, 7, 20, 10, 0),
        extracted_text="body text",
        raw_content="<p>body</p>",
        media_refs=[{"kind": "image", "url": "https://example.org/pic.jpg"}],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _feature(**kw):
    base = dict(
        kind="feature",
        id=100,
        story=None,
        status="published",
        generated_at=datetime(2026, 7, 21, 9, 0),
        quality_score=0.8,
        title="A feature",
        summary="A summary.",
        difficulty="intro",
        reading_time_minutes=4,
        topics=["astro"],
        sections=[{"type": "prose", "text": "hello"}],
        banner_url="https://example.org/banner.jpg",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _aggregate(items=None, **kw):
    items = [_item()] if items is None else items
    story = SimpleNamespace(
        topics=["astro"],
        last_item_at=datetime(2026, 7, 21, 8, 0),
        items=items,
    )
    base = dict(
        kind="aggregate",
        id=200,
        story=story,
        status="published",
        generated_at=datetime(2026, 7, 21, 8, 0),
        quality_score=None,
        title=None,
        summary=None,
        difficulty=None,
        reading_time_minutes=1,
        topics=[],
        sections=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _page(posts, has_more=False, next_cursor=None, feedback_signals=None):
    return {
        "posts": posts,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "feedback_signals": feedback_signals or {},
    }


def _items_page(items, has_more=False, next_cursor=None):
    return {"items": items, "has_more": has_more, "next_cursor": next_cursor}


# --- feed validator -------------------------------------------------------


def test_feed_etag_is_weak_and_stable():
    page = _page([_feature(), _aggregate()])
    etag = _feed_etag(page)
    assert etag.startswith('W/"')
    # Rebuilt from equivalent objects → identical (data, not identity, drives it).
    assert etag == _feed_etag(_page([_feature(), _aggregate()]))


def test_feed_etag_changes_when_a_feature_card_field_changes():
    base = _feed_etag(_page([_feature()]))
    assert _feed_etag(_page([_feature(title="Different")])) != base
    assert _feed_etag(_page([_feature(summary="Other")])) != base
    assert _feed_etag(_page([_feature(banner_url=None)])) != base


def test_feed_etag_ignores_qa_only_changes_a_feature_card_never_shows():
    # A QA revision rewrites body sections + quality_score. The feed card renders
    # neither, so the feed validator must NOT move (the 304 keeps working) even
    # though the post-page validator does (asserted below).
    base = _feed_etag(_page([_feature()]))
    revised = _feature(sections=[{"type": "prose", "text": "rewritten"}], quality_score=0.3)
    assert _feed_etag(_page([revised])) == base


def test_feed_etag_changes_when_an_aggregate_gains_an_item():
    one = _aggregate(items=[_item(id=1)])
    two = _aggregate(items=[_item(id=1), _item(id=2, title="Second")])
    assert _feed_etag(_page([two])) != _feed_etag(_page([one]))


def test_feed_etag_changes_when_the_primary_items_content_changes():
    base = _feed_etag(_page([_aggregate(items=[_item(id=1, title="T")])]))
    changed = _feed_etag(_page([_aggregate(items=[_item(id=1, title="T2")])]))
    assert changed != base


def test_feed_etag_changes_when_an_items_media_changes_the_banner():
    # A re-fetch can add/replace an image on an existing item, moving the derived
    # banner without touching the item count or last_item_at — the etag must see it.
    base = _feed_etag(_page([_aggregate(items=[_item(id=1, media_refs=[])])]))
    with_img = _feed_etag(
        _page([_aggregate(items=[_item(
            id=1, media_refs=[{"kind": "image", "url": "https://example.org/new.jpg"}]
        )])])
    )
    assert with_img != base


def test_feed_etag_tracks_pagination_state():
    posts = [_feature()]
    a = _feed_etag(_page(posts, has_more=False, next_cursor=None))
    b = _feed_etag(_page(posts, has_more=True, next_cursor={"id": 5}))
    assert a != b


# --- post-page validator --------------------------------------------------


def test_post_etag_moves_on_qa_revision_of_a_feature():
    base = _post_page_etag(_feature(), _VOICES, "v1", True)
    revised = _post_page_etag(
        _feature(sections=[{"type": "prose", "text": "rewritten"}], quality_score=0.3),
        _VOICES, "v1", True,
    )
    assert revised != base


def test_post_etag_tracks_narration_controls():
    base = _post_page_etag(_feature(), _VOICES, "v1", True)
    assert _post_page_etag(_feature(), _VOICES, "v1", False) != base            # tts off
    assert _post_page_etag(_feature(), _VOICES, "v2", True) != base             # default voice
    more_voices = _VOICES + [SimpleNamespace(id="v2", label="Two", enabled=True)]
    assert _post_page_etag(_feature(), more_voices, "v1", True) != base         # catalog


def test_aggregate_post_etag_folds_in_story_items():
    base = _post_page_etag(_aggregate(items=[_item(id=1)]), _VOICES, "v1", True)
    grown = _post_page_etag(
        _aggregate(items=[_item(id=1), _item(id=2)]), _VOICES, "v1", True
    )
    assert grown != base
    # An item's rendered content changing (a re-fetch) must move it too.
    edited = _post_page_etag(
        _aggregate(items=[_item(id=1, title="Retitled")]), _VOICES, "v1", True
    )
    assert edited != base


# --- representation distinctness ------------------------------------------
# The boosted-htmx fragment and the full document live at the same URL; their etags
# must differ so a cached full document can never satisfy a fragment conditional
# request (defense-in-depth alongside the response's `Vary: HX-Request`).


def test_feed_fragment_and_full_document_etags_differ():
    page = _page([_feature(), _aggregate()])
    assert _feed_etag(page, fragment=True) != _feed_etag(page, fragment=False)


def test_post_fragment_and_full_document_etags_differ():
    post = _feature()
    full = _post_page_etag(post, fragment=False)
    frag = _post_page_etag(post, fragment=True)
    assert full != frag


# --- infinite-scroll partial validators -----------------------------------
# `/partials/feed` and `/partials/items` have a single representation (always the
# fragment, cursor-keyed), so their validators carry no `fragment` split. The
# invariants mirror the feed's: never a stale 304 (change when the rendered page
# would), hold steady otherwise so the 304 keeps paying off.


def test_feed_partial_etag_is_weak_stable_and_representation_agnostic():
    page = _page([_feature(), _aggregate()])
    etag = _feed_partial_etag(page)
    assert etag.startswith('W/"')
    assert etag == _feed_partial_etag(_page([_feature(), _aggregate()]))
    # Single representation: unlike _feed_etag it takes no `fragment` argument, so
    # there is nothing to make two entries collide under one cursor URL.


def test_feed_partial_etag_changes_when_a_card_changes():
    base = _feed_partial_etag(_page([_feature()]))
    assert _feed_partial_etag(_page([_feature(title="Different")])) != base
    assert _feed_partial_etag(_page([_feature(banner_url=None)])) != base


def test_feed_partial_etag_ignores_qa_only_changes():
    # Same as the feed card: a QA revision rewrites body sections the card never
    # shows, so a deep page's 304 must keep holding.
    base = _feed_partial_etag(_page([_feature()]))
    revised = _feature(sections=[{"type": "prose", "text": "rewritten"}], quality_score=0.3)
    assert _feed_partial_etag(_page([revised])) == base


def test_feed_partial_etag_changes_when_an_aggregate_gains_an_item():
    one = _aggregate(items=[_item(id=1)])
    two = _aggregate(items=[_item(id=1), _item(id=2, title="Second")])
    assert _feed_partial_etag(_page([two])) != _feed_partial_etag(_page([one]))


def test_feed_partial_etag_tracks_pagination_state():
    posts = [_feature()]
    a = _feed_partial_etag(_page(posts, has_more=False, next_cursor=None))
    b = _feed_partial_etag(_page(posts, has_more=True, next_cursor={"id": 5}))
    assert a != b


def test_items_partial_etag_tracks_content_and_pagination():
    base = _items_partial_etag(_items_page([_item(id=1, title="T")]))
    assert _items_partial_etag(_items_page([_item(id=1, title="T2")])) != base
    # A re-fetch adding an image (drives the card banner) must move it.
    assert _items_partial_etag(
        _items_page([_item(id=1, title="T", media_refs=[])])
    ) != base
    # Pagination state is folded in.
    one = _items_partial_etag(_items_page([_item(id=1)], has_more=False))
    two = _items_partial_etag(
        _items_page([_item(id=1)], has_more=True, next_cursor={"id": 9})
    )
    assert one != two


# --- provenance page validator --------------------------------------------


def _prov_post(**kw):
    base = dict(id=100, status="published", archived_at=None, pinned=False,
                quality_score=0.8, generated_at="2026-07-21T09:00:00")
    base.update(kw)
    return base


def _call(cid, post_id=100, pinned=False):
    return {"id": cid, "post_id": post_id, "pinned": pinned}


def test_provenance_etag_is_weak_and_stable():
    post, calls = _prov_post(), [_call(1), _call(2)]
    etag = _provenance_etag(post, calls, fragment=False)
    assert etag.startswith('W/"')
    assert etag == _provenance_etag(_prov_post(), [_call(1), _call(2)], fragment=False)


def test_provenance_etag_moves_on_new_call_pin_or_archival():
    base = _provenance_etag(_prov_post(), [_call(1)], fragment=False)
    # A newly logged (or re-stamped) call.
    assert _provenance_etag(_prov_post(), [_call(1), _call(2)], fragment=False) != base
    # A call pinned in place.
    assert _provenance_etag(_prov_post(), [_call(1, pinned=True)], fragment=False) != base
    # The post pinned / archived / re-scored.
    assert _provenance_etag(_prov_post(pinned=True), [_call(1)], fragment=False) != base
    assert _provenance_etag(
        _prov_post(archived_at="2026-07-22T00:00:00"), [_call(1)], fragment=False
    ) != base
    assert _provenance_etag(_prov_post(quality_score=0.3), [_call(1)], fragment=False) != base


def test_provenance_fragment_and_full_document_etags_differ():
    post, calls = _prov_post(), [_call(1)]
    assert _provenance_etag(post, calls, fragment=True) != _provenance_etag(
        post, calls, fragment=False
    )


# --- feedback state -------------------------------------------------------
# Feedback controls render from the database, so their state is part of what a
# page shows. Both validators have to see it, or the classic cross-page failure
# appears: like a post on its article page, navigate back, and the feed answers
# 304 with the button still drawn as un-pressed.


def test_feed_etag_moves_when_a_card_gains_feedback():
    posts = [_feature()]
    base = _feed_etag(_page(posts))
    liked = _feed_etag(_page(posts, feedback_signals={1: {"kinds": {"like": 9}}}))
    assert liked != base
    # Undo restores the previous validator exactly — nothing residual is hashed.
    assert _feed_etag(_page(posts, feedback_signals={1: {"kinds": {}}})) == base


def test_feed_partial_etag_moves_when_a_card_gains_feedback():
    posts = [_feature()]
    base = _feed_partial_etag(_page(posts))
    liked = _feed_partial_etag(_page(posts, feedback_signals={1: {"kinds": {"save": 3}}}))
    assert liked != base


def test_post_etag_moves_when_the_post_gains_feedback():
    post = _feature()
    base = _post_page_etag(post)
    assert _post_page_etag(post, feedback_ctx={**_NO_FEEDBACK, "signals": {"like": 4}}) != base
    # Topic and source steering render on the article page too, so they count.
    assert _post_page_etag(
        post, feedback_ctx={**_NO_FEEDBACK, "topic_signals": {("astronomy", "more_topic"): 5}}
    ) != base
    assert _post_page_etag(
        post, feedback_ctx={**_NO_FEEDBACK, "source_signals": {7: 6}}
    ) != base
