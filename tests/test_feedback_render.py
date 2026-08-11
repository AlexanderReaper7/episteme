"""Render + wiring checks for the feedback controls and the /tune page.

The controls are pure server state — an active button posts the undo for its own
event id — so the thing worth asserting is that the rendered markup actually
carries that id, and that a template typo can't ship silently.
"""

import re
from datetime import UTC, datetime

from episteme.recommend.profile import ProfileState
from episteme.web.feedback import changed_weights
from episteme.web.templating import templates


def _controls(**overrides):
    context = {
        "post_id": 42,
        "signals": {},
        "topic_signals": {},
        "source_signals": {},
        # Label to read, permanent slug to key signals by.
        "post_topics": [{"label": "astronomy", "slug": "astronomy"}],
        "post_sources": [{"id": 7, "name": "Phys.org"}],
        "variant": "article",
    }
    context.update(overrides)
    return templates.env.get_template("_feedback.html").render(fb=context)


def test_untouched_controls_post_the_record_endpoint():
    html = _controls()
    # `&amp;` because the query context is built as a Jinja value and autoescaped,
    # which is what an ampersand inside an HTML attribute is supposed to look like.
    assert 'hx-post="/feedback?kind=like&amp;post_id=42&amp;variant=article"' in html
    assert 'aria-pressed="false"' in html
    assert "is-active" not in html


def test_recorded_signal_renders_pressed_and_posts_its_own_undo():
    """Undo targets the event id, not the post: that is what makes it exact rather
    than a guess at reversing whatever the profile currently holds."""
    html = _controls(signals={"like": 815})
    assert 'hx-post="/feedback/815/undo?post_id=42&amp;variant=article"' in html
    assert 'aria-pressed="true"' in html
    assert "is-active" in html


def test_every_action_carries_the_variant_it_was_rendered_for():
    """The fragment replaces itself, so a URL that drops `variant` lets the NEXT
    render guess — which is how a feed card used to come back wearing the article
    page's hide-source button."""
    for variant in ("card", "article"):
        html = _controls(
            variant=variant,
            signals={"dislike": 3},
            topic_signals={("astronomy", "less_topic"): 8},
        )
        posts = re.findall(r'hx-post="([^"]+)"', html)
        assert posts, "no controls rendered"
        assert all(f"variant={variant}" in url for url in posts), posts


def test_card_controls_omit_source_steering_but_keep_topic_chips():
    """Hiding an outlet REMOVES its content from the feed, so it stays on the
    article page. Refining a rating by topic is not destructive and belongs
    wherever the rating itself can be given."""
    html = _controls(variant="card", signals={"like": 1})
    assert "hide_source" not in html
    assert "kind=more_topic" in html


def test_topic_chips_appear_only_after_a_rating():
    """Picking a topic means "this part of what I just rated". With no rating there
    is nothing to be more specific about, so there is nothing to offer."""
    assert "more_topic" not in _controls()
    assert "less_topic" not in _controls()


def test_saving_is_not_a_rating_so_it_offers_no_topics():
    """Save is a bookmark, not a statement about the subject."""
    html = _controls(signals={"save": 4})
    assert "more_topic" not in html and "less_topic" not in html


def test_a_chip_inherits_the_direction_of_the_rating():
    """The one-way flow: the reader states a direction once, then names what part
    of the post it was about. A chip can never contradict the rating above it."""
    disliked = _controls(signals={"dislike": 2})
    assert "kind=less_topic" in disliked and "kind=more_topic" not in disliked
    assert "Less of" in disliked

    liked = _controls(signals={"like": 2})
    assert "kind=more_topic" in liked and "kind=less_topic" not in liked
    assert "More of" in liked


def test_topic_steering_posts_the_slug_and_reads_the_label():
    """The chip shows the vocabulary's spelling but files the signal under the
    topic's permanent slug, so a rename can neither redirect the click onto a
    different topic nor detach the chip from the event it renders from."""
    html = _controls(
        signals={"dislike": 1},
        post_topics=[
            {"label": "the colour blue", "slug": "blue"},
            {"label": "optics", "slug": "optics"},
        ],
        topic_signals={("blue", "more_topic"): 12},
    )
    assert "the colour blue" in html
    assert "topic=optics" in html and "kind=less_topic" in html
    assert 'hx-post="/feedback/12/undo?post_id=42&amp;variant=article"' in html


def test_a_picked_chip_survives_undoing_the_rating_that_gave_it_meaning():
    """The orphan case. The topic event still exists, so its chip stays visible and
    still undoable — hiding it would make a recorded signal unreachable. The
    UNPICKED chips go, along with the direction they would have carried."""
    html = _controls(
        topic_signals={("astronomy", "less_topic"): 55},
        post_topics=[
            {"label": "astronomy", "slug": "astronomy"},
            {"label": "optics", "slug": "optics"},
        ],
    )
    assert 'hx-post="/feedback/55/undo?post_id=42&amp;variant=article"' in html
    assert "optics" not in html
    assert "kind=less_topic" not in html


def test_hiding_a_source_is_confirmed_because_it_removes_content():
    """Blocks filter rather than down-rank, so the label must not undersell it."""
    html = _controls()
    assert "hx-confirm=" in html
    assert "Phys.org" in html


def test_hidden_source_offers_unhide_without_a_confirm():
    html = _controls(source_signals={7: 99})
    assert 'hx-post="/feedback/99/undo?post_id=42&amp;variant=article"' in html
    assert "Show everything from Phys.org" in html


def test_every_icon_only_control_names_itself():
    """A glyph with no words is unreadable without both a tooltip (`title`, what
    the user asked for) and an accessible name (`aria-label`). The chips are
    exempt: their content IS the word."""
    html = _controls(signals={"like": 1})
    for button in re.findall(r"<button\b.*?</button>", html, re.S):
        if "feedback-btn--topic" in button:
            continue
        assert re.search(r'title="[^"]+"', button), button
        assert re.search(r'aria-label="[^"]+"', button), button


def _tune(**overrides):
    context = {
        "profile": ProfileState(),
        "events": [],
        "posts": {},
        "sources": {},
        # (slug, current label, weight) — the panel reads labels, the form posts slugs.
        "topic_rows": [],
        "topic_labels": {},
        "vocabulary": [],
        "blockable_sources": [],
        "clamp": 6.0,
        "echo": None,
        "error": None,
        "topic_error": None,
        "block_error": None,
        "saved": 0,
    }
    context.update(overrides)
    return templates.env.get_template("_tune_panel.html").render(**context)


def test_cold_profile_says_so_rather_than_showing_an_empty_chart():
    html = _tune()
    assert "Nothing learned yet" in html
    assert "No signals recorded yet" in html


def _weights():
    return _tune(
        profile=ProfileState(topic_weights={"marine-biology": 2.4, "ai-hype": -3.0}),
        topic_rows=[
            ("ai-hype", "ai hype", -3.0),
            ("marine-biology", "marine biology", 2.4),
        ],
    )


def test_weights_render_as_sliders_with_direction_and_value():
    html = _weights()
    assert "marine biology" in html
    assert 'type="range"' in html
    assert 'value="2.4"' in html and 'value="-3.0"' in html
    assert "+2.4" in html and "-3.0" in html  # the readout keeps its sign
    # Direction is carried by the centre-anchored fill, not by a minus sign alone.
    assert "--fill-color: var(--accent)" in html
    assert "--fill-color: var(--bad-muted)" in html


def test_every_slider_is_one_form_committed_by_save():
    """Dragging several sliders is ONE editing session: the whole list posts once,
    to the batch endpoint, and only when the reader says so."""
    html = _weights()
    assert 'hx-post="/tune/weights"' in html
    assert html.count('hx-post="/tune/weights"') == 1
    assert 'name="w:marine-biology"' in html and 'name="w:ai-hype"' in html
    assert 'name="dirty"' in html
    # Nothing to save until something moves, so the button starts inert.
    assert "weight-save" in html and "disabled" in html


def test_sliders_carry_their_starting_value_so_untouched_ones_stay_untouched():
    """`data-initial` is what makes "changed" exact on the client — without it an
    untouched slider could mint a set_topic pinning a weight nobody moved."""
    html = _weights()
    assert 'data-initial="2.4"' in html
    assert 'data-initial="-3.0"' in html


def test_the_slider_range_is_the_clamp_the_weights_are_bounded_by():
    """The scale the reader drags on must be the scale replay enforces, or the
    panel would accept a value it then silently changes."""
    html = _tune(clamp=3.0, topic_rows=[("genetics", "genetics", 1.0)])
    assert 'min="-3.0"' in html and 'max="3.0"' in html


def test_a_slider_reads_as_the_vocabularys_current_spelling():
    """The weight is filed under the slug it was learned against; the panel must
    still show what that topic is called today, not what it was called then."""
    html = _tune(topic_rows=[("blue", "the colour blue", 1.0)])
    assert "the colour blue" in html
    assert 'name="w:blue"' in html


def test_the_slider_list_says_it_needs_javascript():
    """Which sliders moved is knowable only on the client, so a no-JS submit
    records nothing — the panel has to say so and point at the box that works."""
    html = _tune(topic_rows=[("genetics", "genetics", 1.0)])
    assert "<noscript>" in html
    assert "need JavaScript" in html


def test_a_topic_can_be_set_from_the_vocabulary_even_with_no_slider_yet():
    """Cold start: there are no sliders to drag, so the box is the only way in."""
    html = _tune(vocabulary=["deep-sea biology"])
    assert 'hx-post="/tune/topic"' in html
    assert '<option value="deep-sea biology">' in html


def test_dragging_a_weight_says_it_replaces_rather_than_nudges():
    """The one thing a reader must not get wrong about this control."""
    assert "absolute" in _tune()
    assert "nothing is recorded until you save" in _tune()


def test_a_saved_batch_is_confirmed_with_a_count():
    rows = [("genetics", "genetics", 1.0)]
    assert "Saved 2 weights" in _tune(saved=2, topic_rows=rows)
    assert "Saved 1 weight." in _tune(saved=1, topic_rows=rows)


def test_a_bad_weight_is_reported_next_to_the_weights_not_the_statement_box():
    html = _tune(topic_error="“abc” is not a number")
    assert 'role="alert"' in html
    assert "is not a number" in html


# --- Hard blocks ------------------------------------------------------------------


def test_each_block_is_listed_with_a_way_to_remove_it():
    html = _tune(
        profile=ProfileState(blocked_keywords=["crypto"], blocked_sources=[7]),
        sources={7: "Phys.org"},
    )
    assert 'hx-post="/tune/unblock?keyword=crypto"' in html
    assert 'hx-post="/tune/unblock?source_id=7"' in html


def test_an_empty_block_list_says_so_rather_than_hiding_the_controls():
    """The section has to be reachable when nothing is blocked — it is now the way
    blocks are ADDED, not just a readout of ones a statement produced."""
    html = _tune()
    assert "Nothing is blocked." in html
    assert 'name="keyword"' in html


def test_a_blocked_outlet_is_not_offered_for_blocking_again():
    html = _tune(
        profile=ProfileState(blocked_sources=[7]),
        sources={7: "Phys.org", 9: "Nature"},
        blockable_sources=[{"id": 9, "name": "Nature"}],
    )
    assert '<option value="9">Nature</option>' in html
    assert '<option value="7">' not in html


def test_the_outlet_picker_is_omitted_when_everything_is_blocked():
    html = _tune(profile=ProfileState(blocked_sources=[7]), sources={7: "Phys.org"})
    assert 'aria-label="outlet to block"' not in html


def test_a_block_error_is_reported_in_the_blocks_section():
    html = _tune(block_error="Name a word or pick an outlet")
    assert "Name a word or pick an outlet" in html


# --- Which sliders a Save actually commits ----------------------------------------


def test_only_moved_sliders_are_recorded():
    """The browser posts every slider. Recording them all would pin a weight for
    every topic on the page the moment the reader touched one of them."""
    changed = changed_weights({"genetics": "3.0", "ecology": "1.0"}, "genetics")
    assert changed == {"genetics": "3.0"}


def test_an_empty_dirty_field_commits_nothing():
    """A submit with nothing moved is a no-op, not "save everything as rendered"."""
    assert changed_weights({"genetics": "3.0"}, "") == {}


def test_dirty_is_the_only_answer_not_a_value_comparison():
    """Setting a topic to the value it already shows is a real edit — it pins an
    accumulated weight — so the client's word is what counts, not whether the
    number changed."""
    assert changed_weights({"genetics": "1.0"}, "genetics") == {"genetics": "1.0"}


def test_a_submit_without_dirty_records_nothing():
    """No `dirty` at all means no JavaScript, and the server cannot reconstruct
    which sliders moved: it only knows the stored weights, which drift past the
    slider's rounding on their own as signals decay, so "differs from stored"
    would record sliders nobody touched. Recording nothing is the honest answer,
    and the panel says so in a <noscript> pointing at the box that does work."""
    assert changed_weights({"genetics": "2.0", "ecology": "-4.5"}, None) == {}


def test_echo_is_shown_so_the_interpretation_is_confirmable():
    html = _tune(echo="Got it — more marine biology, less AI hype.")
    assert "Got it" in html
    assert 'role="status"' in html


def test_parse_failure_is_shown_in_place():
    html = _tune(error="Could not interpret that right now")
    assert 'role="alert"' in html
    assert "Could not interpret" in html


def test_blocks_are_labelled_as_removing_content():
    html = _tune(
        profile=ProfileState(blocked_keywords=["crypto"], blocked_sources=[7]),
        sources={7: "Phys.org"},
    )
    assert "Hard blocks" in html
    assert "crypto" in html and "Phys.org" in html
    assert "remove content from the feed entirely" in html


def test_every_signal_offers_undo():
    from episteme.models import Feedback

    events = [
        Feedback(
            id=5,
            kind="nl_feedback",
            created_at=datetime(2026, 7, 29, tzinfo=UTC),
            nl_text="more marine biology",
            source_ids=[],
            topics_snapshot=[],
        )
    ]
    html = _tune(events=events)
    assert 'hx-post="/feedback/5/undo"' in html
    assert "more marine biology" in html
