"""The header nav below 40rem: one list, folded behind a button (0053).

The failure this guards against is not a crash. A second copy of the links for
the narrow layout renders perfectly and drifts silently - a route added to one
list and not the other is invisible until someone opens the app on a phone and
cannot get to it. So the property is that there is exactly ONE nav in the
markup, and the menu is a second layout of it.
"""

import re

from episteme.web.templating import BASE_DIR, templates

CSS = (BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")
JS = (BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
BASE = (BASE_DIR / "templates" / "base.html").read_text(encoding="utf-8")

NARROW = CSS.split("@media (max-width: 40rem) {\n  .site-menu-toggle {")[1].split("\n}\n")[0]
# The declarations alone. Several of these rules explain themselves by NAMING the
# thing they refuse, so a substring check that reads the comments finds the exact
# opposite of what it is looking for.
NARROW_CODE = re.sub(r"/\*.*?\*/", "", NARROW, flags=re.S)


def _page() -> str:
    return templates.env.get_template("base.html").render()


def test_the_links_exist_once():
    """One list, two layouts. A `<nav>` per breakpoint is the bug this file is
    about: both render, and only one of them gets the next route."""
    page = _page()
    assert page.count('<nav class="site-nav"') == 1
    for href in ('href="/glance"', 'href="/tune"', 'href="/admin"'):
        assert page.count(href) == 1


def test_the_toggle_is_a_button_that_names_the_nav():
    """An anchor would be boosted and would navigate; a `<details>` hides its own
    content when closed, which is what the wide layout must not do."""
    page = _page()
    assert '<button class="site-menu-toggle" type="button"' in page
    assert 'aria-controls="site-nav"' in page
    assert 'id="site-nav"' in page
    # Icon-only, so the button carries the accessible name itself (see _icons.html).
    assert 'aria-label="Menu"' in page


def test_the_button_does_not_exist_on_a_wide_window():
    """`display: none` and not `visibility: hidden`: the header is a baseline row
    and a reserved-but-empty box would push the nav off the right edge."""
    rule = CSS.split(".site-menu-toggle {")[1].split("}")[0]
    assert "display: none;" in rule
    assert "display: inline-flex;" in NARROW


def test_the_open_state_only_means_anything_when_narrow():
    """The class stays on the nav when the window grows - nothing removes it - so
    the rule that reads it has to live inside the query, or a menu opened on a
    phone would leave the desktop nav stacked in a panel."""
    assert ".site-nav.is-open" not in CSS.split("@media (max-width: 40rem) {")[0]
    assert ".site-nav.is-open { display: flex; }" in NARROW


def test_the_panel_is_opaque():
    """It hangs outside the header's box, and the header is already a backdrop
    root, so the app's frost recipe renders as plain transparency over whatever
    photo is behind it. Measured 2026-08-31: unblurred, at `blur(10px)`."""
    assert "backdrop-filter" not in NARROW_CODE
    assert "var(--frost-bg)" not in NARROW_CODE
    assert "background: var(--card-bg);" in NARROW_CODE


def test_every_way_of_closing_it():
    """A boosted link swaps `#main-content` and never touches the header, so
    without an explicit close the panel would stay open over the page it just
    navigated to."""
    body = JS.split("/* ========================= the header menu ")[1].split(
        "/* ====================="
    )[0]
    assert 'e.target.closest("[data-site-menu]")' in body  # the button toggles
    assert 'if (nav.classList.contains("is-open")) setSiteMenu(false);' in body
    assert 'e.key !== "Escape"' in body
    # NOT htmx:load: a polling fragment anywhere on the page fires it, and the
    # menu would close under the reader's finger.
    assert 'addEventListener("htmx:load"' not in body


def test_the_glyphs_are_the_sprites_and_swap_on_the_button():
    """The icon's identity stays in the markup and the sprite. A script that
    rewrote `<use href>` would put it in three places."""
    page = _page()
    assert re.search(r'site-menu-glyph site-menu-glyph--closed"[^>]*>\s*<use href="#i-menu"', page)
    assert re.search(r'site-menu-glyph site-menu-glyph--open"[^>]*>\s*<use href="#i-remove"', page)
    assert (
        '.site-menu-toggle[aria-expanded="true"] .site-menu-glyph--closed { display: none; }' in CSS
    )
