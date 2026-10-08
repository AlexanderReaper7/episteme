"""The Glance page: what core lays out, and what it refuses to guess (0046).

The blocks themselves are a correspondent's business. What is asserted here is
core's half: one block per enabled correspondent, pointed at the path the mount
actually serves, and a failure line that is in the document before anything has
failed, because htmx will not swap a body into a block whose request errored.
"""

from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from episteme.correspondents import registry
from episteme.web import glance as glance_module
from episteme.web.app import app


def row(slug, label, enabled=True):
    return SimpleNamespace(slug=slug, label=label, enabled=enabled)


def plugin(slug, stylesheet=None):
    return SimpleNamespace(
        slug=slug, label=slug, router=APIRouter(), stylesheet=stylesheet, templates=None
    )


@pytest.fixture
def page(monkeypatch):
    """Render /glance over a fabricated set of correspondent rows."""

    def build(rows, plugins=()):
        class _Session:
            async def execute(self, _statement):
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(glance_module, "SessionLocal", lambda: _Session())
        monkeypatch.setattr(registry, "_PLUGINS", {p.slug: p for p in plugins})
        response = TestClient(app).get("/glance")
        assert response.status_code == 200
        return response.text

    return build


def test_one_block_per_enabled_correspondent(page):
    html = page([row("matsedel", "Matsedel"), row("trains", "Departures")])
    assert html.count('class="glance-block ') == 2
    assert 'hx-get="/c/matsedel/glance"' in html
    assert 'hx-get="/c/trains/glance"' in html
    # No trailing slash: the plugin's index route is `""`, and `/c/<slug>/` is a
    # 307 to it.
    assert 'href="/c/matsedel"' in html
    assert ">Departures<" in html


def test_a_block_carries_the_correspondents_scoping_class(page):
    """The same modifier as the correspondent's own page, so one additive
    stylesheet covers both. Not `.correspondent-page`, which is page padding."""
    html = page([row("matsedel", "Matsedel")])
    assert 'class="glance-block correspondent--matsedel"' in html
    assert "correspondent-page" not in html


def test_the_failure_line_is_rendered_before_anything_fails(page):
    """htmx leaves a 4xx/5xx body unswapped, so the block cannot be told what to
    say after the fact: the words are already there, hidden."""
    html = page([row("matsedel", "Matsedel")])
    assert 'class="glance-block-failed" hidden' in html
    assert "Matsedel is not responding." in html
    assert "data-glance-retry" in html


def test_the_retry_control_is_a_button_and_not_a_boosted_anchor(page):
    """Every <a> here is boosted, and `preventDefault` on the bubbled click does
    not stop htmx: an `href="#"` fired a second request that swapped
    #main-content and wiped the blocks that had loaded."""
    html = page([row("matsedel", "Matsedel")])
    assert '<button type="button" class="glance-retry" data-glance-retry>' in html
    assert 'href="#"' not in html


def test_a_block_does_not_govern_the_html_it_is_handed(page):
    """The block body declares `hx-target="this"` so its own fetch swaps into
    itself, and htmx resolves hx-target by walking up from the triggering
    element. Without `hx-disinherit`, every boosted <a> a correspondent puts in
    its block finds that target first and swaps its own page - header, sprite,
    chat rail and all - into the block instead of into #main-content.

    Watched happening on 2026-08-28, the first time a block carried a link to
    its correspondent's page. The rule this pins: an element that swaps in HTML
    core did not author must not hand that HTML its own htmx attributes."""
    html = page([row("matsedel", "Matsedel")])
    body = html[html.index('class="glance-block-body"') :]
    body = body[: body.index(">")]
    assert 'hx-target="this"' in body
    assert 'hx-disinherit="*"' in body


def test_a_row_with_no_plugin_still_gets_a_block(page):
    """That is what an external correspondent is (0046). Its block fails to load
    rather than vanishing from the page."""
    html = page([row("weather", "Weather")], plugins=())
    assert 'hx-get="/c/weather/glance"' in html
    assert 'href="/c/weather/style.css"' not in html


def test_only_a_correspondent_that_ships_css_gets_a_link(page):
    html = page(
        [row("matsedel", "Matsedel"), row("trains", "Departures")],
        plugins=[plugin("matsedel", stylesheet=object()), plugin("trains")],
    )
    assert 'href="/c/matsedel/style.css"' in html
    assert 'href="/c/trains/style.css"' not in html


def test_nothing_enabled_is_an_empty_state_not_an_empty_page(page):
    html = page([])
    assert "No correspondents are enabled" in html
    assert "glance-block-body" not in html


def test_glance_is_in_the_navbar(page):
    html = page([])
    assert '<a href="/glance">' in html
