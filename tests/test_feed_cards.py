"""What the markup of a feed card has to keep being true.

Three properties, all about a card that is one big stretched link:

  * the expand control opens the full summary in the feed (0050). The card shows
    as many lines as it has room for and fades the rest - an article still has
    its page behind it, but an aggregate card links straight OUT, so clamped
    text on one of those had nowhere to be read at all.
  * the picture is a third of the card, and it moves above the text when the
    window is taller than it is wide (0052).
  * an off-site destination is never prefetched. The hover prefetch is the one
    place the app can reach a source outside `polite_get` (0005) without anyone
    writing a request.
"""

from types import SimpleNamespace

import pytest

from episteme.web.templating import BASE_DIR, templates

CSS = (BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")
JS = (BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")


def _post(kind: str, summary: str | None = "A summary that runs long.") -> SimpleNamespace:
    source = SimpleNamespace(name="Phys.org", type_name="rss")
    item = SimpleNamespace(
        title="An item", url="https://example.org/a", source=source, media_refs=None
    )
    story = SimpleNamespace(items=[item], topics=[], last_item_at=None)
    return SimpleNamespace(
        id=42,
        kind=kind,
        title="A title",
        summary=summary,
        story=story,
        href=None,
        topics=[],
        banner_url=None,
        generated_at=None,
        publish_at=None,
        reading_time_minutes=5,
        difficulty="intermediate",
    )


def _render(post) -> str:
    return templates.env.get_template("_feed.html").render(
        posts=[post],
        feedback_contexts={
            42: {
                "post_id": 42,
                "signals": {},
                "topic_signals": {},
                "source_signals": {},
                "post_topics": [],
                "post_sources": [],
                "variant": post.kind,
            }
        },
        correspondent_labels={},
        has_more=False,
        next_cursor=None,
        first_page=True,
    )


@pytest.mark.parametrize("kind", ["article", "aggregate", "filed"])
def test_every_card_kind_gets_the_control(kind):
    """The reason `_card_summary.html` is a file and not three copies: a card kind
    whose summary could not be opened would be one nobody noticed for months."""
    html = _render(_post(kind))
    assert "data-card-expand" in html
    assert 'aria-controls="card-summary-42"' in html
    assert 'id="card-summary-42"' in html


def test_the_control_is_a_button_not_an_anchor():
    """Every anchor on the page is boosted. A boosted `href="#"` fires its own
    request and swaps #main-content out from under the feed, and preventDefault
    does not stop it - htmx has already taken the click."""
    html = _render(_post("article"))
    assert '<button class="card-expand" type="button"' in html


def test_a_post_with_no_summary_renders_no_control():
    html = _render(_post("filed", summary=None))
    assert "card-expand" not in html
    assert "card-snippet" not in html


def test_the_control_lifts_above_the_cards_stretched_link():
    """The whole card is one stretched anchor. Without the z-index lift the click
    navigates to the post instead of opening the summary - the exact failure the
    control exists to avoid."""
    rule = CSS.split(".card-expand {")[1].split("}")[0]
    assert "z-index: 2" in rule
    assert "position: relative" in rule


def test_the_control_is_hidden_until_the_text_is_measured():
    """Shown by default it would flash onto every card in the feed and then be
    taken off most of them, since the measurement only exists after paint."""
    rule = CSS.split(".card-expand {")[1].split("}")[0]
    assert "display: none" in rule
    assert ".card-snippet.is-clamped ~ .card-expand { display: inline-flex; }" in CSS


def test_an_open_summary_is_not_re_measured():
    """`.is-clamped` means the text overflows, not that it is currently hidden. An
    expanded snippet clamps at nothing, so re-measuring it answers "it fits" and
    removes the button that closes it again."""
    body = JS.split("function markClampedText(root)")[1].split("\n  }")[0]
    assert 'classList.contains("is-expanded")' in body
    assert "return;" in body


# --- what a card is allowed to fetch --------------------------------------------


def test_an_offsite_card_is_never_prefetched():
    """`preload="mouseover"` is inherited from <body>, so every card link is a
    prefetch candidate. Off-site there is nothing of ours to prefetch, and firing
    it would be a GET at the source on every hover, outside `polite_get` (0005)."""
    post = _post("aggregate")
    post.href = "https://www.nasa.gov/news-release/artemis/"
    assert 'preload="none"' in _render(post)


def test_an_on_site_card_still_prefetches():
    """The guard is on the destination, not on the kind: an aggregate that points
    back into Episteme is an ordinary boosted link and should stay fast."""
    assert 'preload="none"' not in _render(_post("article"))


def test_the_offsite_card_stays_boosted():
    """Reading `hx-boost="false"` as the fix is the trap. The preload extension
    branches on boost: a boosted link goes through `htmx.ajax`, where
    `selfRequestsOnly` rejects the cross-origin URL and logs `htmx:invalidPath` -
    the guard working. An UNBOOSTED one goes through a raw XMLHttpRequest that no
    guard sees. Turning boost off would start the very request this prevents."""
    post = _post("aggregate")
    post.href = "https://www.nasa.gov/news-release/artemis/"
    assert "hx-boost" not in _render(post)


# --- how the card divides its space (0052) --------------------------------------


def test_the_picture_is_a_third_of_the_card():
    """A fraction, not a width. Every literal that stood here was a thumbnail on
    one screen and a wall on another, and the `vw` clamp before it measured the
    window rather than the card it sits in."""
    assert ".card:has(.card-banner) {" in CSS
    rule = CSS.split(".card:has(.card-banner) {")[1].split("}")[0]
    assert "grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);" in rule


def test_a_card_with_no_picture_takes_one_column():
    """`onerror` REMOVES a dead banner, so this is not only the no-image case: a
    two-track card whose image 404s would keep a third of itself empty. The
    two-track rule is the one behind `:has()` for exactly that reason."""
    rule = CSS.split("\n.card {")[1].split("}")[0]
    assert "grid-template-columns: minmax(0, 1fr);" in rule
    # `cqi` in the banner's height resolves against the card, so the card has to
    # declare itself a container or the height silently falls back to 0.
    assert "container-type: inline-size;" in rule


def test_a_portrait_window_stacks_the_picture_on_top():
    """The question is the proportions of the space, not the class of device, so
    the query is `orientation` and not a pixel breakpoint - a narrow window on a
    wide monitor gets the same answer as a phone."""
    block = CSS.split("@media (orientation: portrait) {")[1].split("\n}\n")[0]
    assert "grid-template-columns: minmax(0, 1fr);" in block
    assert "aspect-ratio: 16 / 9;" in block
    assert "grid-row: 2;" in block  # the body, under the picture


def test_the_clamp_is_measured_not_a_constant():
    """Nine lines was measured once at 2560px against a layout that no longer
    exists, and it was wrong for every other window - four lines is what the same
    card can afford on a phone with the picture stacked above it."""
    rule = CSS.split(".card-snippet {")[1].split("}")[0]
    assert "-webkit-line-clamp: var(--card-snippet-lines, 9);" in rule
    assert "--card-snippet-lines" in JS


def test_the_fit_reserves_room_for_the_expand_button():
    """The button is `display: none` until the text is known to overflow, so on
    the pass that decides the line count it measures zero. Without a reserve the
    card asks for one line more than it has and the button lands off the bottom
    of the picture beside it."""
    body = JS.split("function fitSnippet(snippet)")[1].split("\n  }")[0]
    assert "Math.max(outerHeight(part), lineHeight)" in body
    # The budget is the window, which is what makes a card readable without
    # scrolling past its own rating controls.
    assert "window.innerHeight" in body
