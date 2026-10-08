"""The htmx fragment primitive (web.templating.render): a boosted navigation must
get back ONLY the targeted block (with a <title> so the tab stays in sync), while a
normal request — a direct hit, a bookmark, a hard refresh — gets the full document.
This is what makes "just the part that changes" go over the wire without breaking
non-htmx access.

admin_runs.html is used because its context is plain data (no DB), so we can exercise
all three render modes as a unit test."""

from starlette.requests import Request

from episteme.web.templating import render

# Fabricated, DB-free context for admin_runs.html.
CTX = {
    "active": "runs",
    "summary": {"running": [], "last": None, "next_fire": None},
    "runs": [],
}


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/runs",
            "query_string": b"",
            "headers": raw,
        }
    )


def _body(response) -> str:
    return response.body.decode()


def test_plain_request_renders_full_document():
    html = _body(render(_request({}), "admin/admin_runs.html", CTX))
    assert "<!DOCTYPE html>" in html or "<html" in html
    assert "admin-sidebar-nav" in html  # shell present
    assert "Pipeline runs" in html  # page content present


def test_boosted_admin_nav_returns_inner_fragment_only():
    html = _body(
        render(
            _request({"HX-Request": "true", "HX-Target": "admin-main"}),
            "admin/admin_runs.html",
            CTX,
        )
    )
    assert "<html" not in html and "<body" not in html  # no document chrome
    assert "admin-sidebar-nav" not in html  # sidebar NOT re-sent
    assert "<title>Episteme • Pipeline runs</title>" in html  # title for htmx
    assert 'class="admin-title"' in html  # the admin_content block


def test_boosted_public_nav_returns_content_fragment():
    # A public page defines `content` at leaf level, so a #main-content target emits
    # just that block. feed.html renders DB-free with an empty feed.
    feed_ctx = {
        "feed": {"posts": [], "has_more": False, "next_cursor": None, "first_page": True},
        "fallback": None,
    }
    html = _body(
        render(
            _request({"HX-Request": "true", "HX-Target": "main-content"}),
            "feed.html",
            feed_ctx,
        )
    )
    assert "<html" not in html and "<body" not in html
    assert "<title>Episteme • Feed</title>" in html
    assert 'id="feed"' in html  # the content block


def test_admin_main_content_target_falls_back_to_full_document():
    # The admin shell's `content` block is inherited (in admin_base), not defined in
    # the leaf, so it can't be emitted as a fragment — render() falls back to the full
    # document rather than a half page. (This path isn't hit in practice: entering
    # admin is a full load and intra-admin nav targets #admin-main.)
    html = _body(
        render(
            _request({"HX-Request": "true", "HX-Target": "main-content"}),
            "admin/admin_runs.html",
            CTX,
        )
    )
    assert "<html" in html and "admin-sidebar-nav" in html


def test_history_restore_request_gets_the_whole_document():
    # htmx's Back/Forward restore (`loadHistoryFromServer`) sends HX-Request WITHOUT
    # HX-Target and swaps the result in as the page body — so it must get a full
    # document, not the `content` block. This is the request the fragment/full-doc
    # split used to mis-tag: same body as a plain hit, but stamped with the
    # FRAGMENT's etag, which nested the next boosted navigation inside itself.
    html = _body(
        render(
            _request({"HX-Request": "true", "HX-History-Restore-Request": "true"}),
            "admin/admin_runs.html",
            CTX,
        )
    )
    assert "<html" in html and "admin-sidebar-nav" in html


def test_htmx_request_without_known_target_falls_back_to_full_document():
    # A boosted request whose target maps to no block (defensive) must not 500 or
    # emit a half page — it falls through to the full document.
    html = _body(
        render(
            _request({"HX-Request": "true", "HX-Target": "something-else"}),
            "admin/admin_runs.html",
            CTX,
        )
    )
    assert "<html" in html and "admin-sidebar-nav" in html
