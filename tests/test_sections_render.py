"""Render smoke test: post.html must render every section type without error and
with the right markers — a template typo here would otherwise only surface on a
live post page (or a QA screenshot) at 3 AM."""

import copy
import re
from datetime import UTC, datetime

from episteme.models import Post
from episteme.web.templating import templates

ALL_SECTIONS = [
    {"type": "prose", "text": "**bold** body"},
    {"type": "key_points", "items": ["point one", "point two"]},
    {"type": "image", "url": "https://cdn.example.org/webb.jpg", "caption": "The nebula",
     "attribution": "ESA Webb", "source_url": "https://esa.int/a1"},
    {"type": "video", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
     "caption": "Flyover", "attribution": "NASA", "source_url": "https://nasa.gov/a2"},
    {"type": "quiz", "questions": [
        {"question": "Why is the sky blue?",
         "choices": ["Rayleigh scattering", "Ozone"], "answer_index": 0,
         "explanation": "Shorter wavelengths scatter more."}]},
    {"type": "chart", "spec": {"mark": "bar", "data": {"values": [{"x": "a", "y": 1}]}},
     "caption": "Counts"},
    {"type": "diagram", "mermaid": "flowchart LR; A --> B", "caption": "Flow"},
    {"type": "timeline", "events": [{"date": "1969", "label": "Apollo 11"},
                                    {"date": "2026", "label": "Artemis II"}]},
    {"type": "glossary", "terms": [{"term": "quasar", "definition": "a luminous AGN"}]},
    {"type": "sources", "items": [{"title": "Src", "url": "https://s.org/1", "outlet": "S"}]},
    {"type": "further_reading", "items": [{"title": "More", "url": "https://m.org/1",
                                           "outlet": "M"}]},
]


def _render(sections):
    post = Post(
        id=1, kind="article", title="T", summary="S", difficulty="intermediate",
        topics=["astronomy"], sections=sections, reading_time_minutes=3,
        generated_at=datetime(2026, 7, 19, tzinfo=UTC), model_used="test-model",
    )
    # `_feedback.html` is a strict component — it renders from the database state
    # its route supplies and has no defaults, so a caller that forgets the context
    # fails loudly rather than silently drawing every button as un-pressed. This
    # test is about sections, so it supplies the "no signals yet" shape.
    return templates.env.get_template("post.html").render(
        post=post,
        fb={
            "post_id": post.id,
            "signals": {},
            "topic_signals": {},
            "source_signals": {},
            "post_topics": [{"label": t, "slug": t} for t in post.topics],
            "post_sources": [],
            "variant": "article",
        },
    )


def test_all_section_types_render():
    html = _render(ALL_SECTIONS)
    assert "<strong>bold</strong>" in html                                # prose markdown
    assert "point one" in html                                            # key_points
    assert 'src="https://cdn.example.org/webb.jpg"' in html               # image hotlink
    assert "ESA Webb" in html                                             # stamped attribution
    assert 'src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in html  # whitelist embed
    assert "data-answer=" in html and "Rayleigh scattering" in html       # quiz
    assert "data-spec=" in html and "chart-target" in html                # chart
    assert 'class="mermaid"' in html and "flowchart LR" in html           # diagram
    assert "Apollo 11" in html                                            # timeline
    assert "quasar" in html                                               # glossary
    assert "https://s.org/1" in html and "https://m.org/1" in html        # citation tails


def test_sections_expose_hydration_markers_not_inline_scripts():
    # Rich sections are no longer wired up by per-page <script> tags; the persistent
    # app.js hydrates them on htmx:load, lazy-loading vega/mermaid AT RUNTIME only when
    # it sees the markers below. base.html always carries a `window.EPISTEME_ASSETS`
    # map of fingerprinted vendor URLs (app.js resolves its dynamic imports through
    # it), so the filenames appear as map VALUES on every page — that is not an eager
    # load. The HTML-level invariant is that no heavy renderer is pulled in by a
    # `<script src>` tag (that would defeat both boosted swaps and lazy loading).
    with_chart = _render([{"type": "chart", "spec": {"mark": "bar"}}])
    assert "section-chart" in with_chart and "data-spec=" in with_chart
    assert 'src="/static/vendor/vega' not in with_chart
    assert "post.js" not in with_chart

    with_diagram = _render([{"type": "diagram", "mermaid": "flowchart LR; A --> B",
                             "caption": "c"}])
    assert 'class="mermaid"' in with_diagram
    assert 'src="/static/vendor/mermaid.min.js' not in with_diagram

    with_quiz = _render([{"type": "quiz", "questions": [
        {"question": "q", "choices": ["a", "b"], "answer_index": 1, "explanation": "e"}]}])
    assert "section-quiz" in with_quiz and "data-answer=" in with_quiz

    prose_only = _render([{"type": "prose", "text": "just text"}])
    assert "section-chart" not in prose_only and "section-quiz" not in prose_only
    # No eager renderer script tags either (the fingerprint map may still name them).
    assert 'src="/static/vendor/vega' not in prose_only
    assert 'src="/static/vendor/mermaid.min.js' not in prose_only


MULTI_QUIZ = {"type": "quiz", "questions": [
    {"question": "Why is the sky blue?", "choices": ["Rayleigh scattering", "Ozone"],
     "answer_index": 0, "explanation": "Shorter wavelengths scatter more."},
    {"question": "Why are sunsets red?", "choices": ["Dust", "Longer path length", "Ozone"],
     "answer_index": 1, "explanation": "Blue is scattered out of the line of sight."},
]}


def _quiz_answers(html):
    """[(marked answer text, all choice texts)] for each rendered question."""
    out = []
    for block in html.split('class="quiz-item"')[1:]:
        answer = int(re.search(r'data-answer="(\d+)"', block).group(1))
        # Each choice leads with a hidden correct/wrong mark, so the label is
        # whatever follows the last tag inside the button.
        choices = [
            re.sub(r"(?s).*>", "", body).strip()
            for body in re.findall(
                r'class="quiz-choice" data-index="\d+"\s*>(.*?)</button>', block, re.S
            )
        ]
        out.append((choices[answer], choices))
    return out


def test_multi_question_quiz_renders_one_check_with_numbered_items():
    html = _render([MULTI_QUIZ])
    assert html.count('class="quiz-item"') == 2
    assert html.count("Check your understanding") == 1   # one heading, not one per question
    assert "Question 1 of 2" in html and "Question 2 of 2" in html
    assert "Shorter wavelengths scatter more." in html   # per-question explanation


def test_single_question_quiz_omits_numbering():
    html = _render([ALL_SECTIONS[4]])
    assert html.count('class="quiz-item"') == 1
    assert "Question 1 of 1" not in html


def test_quiz_never_scores_the_reader():
    """Questions resolve one by one and nothing is tallied across them — a score
    is a grade, and this feed has no reward mechanics (spec §1)."""
    html = _render([MULTI_QUIZ])
    assert "quiz-score" not in html
    assert "correct." not in html.replace("quiz-correct", "")


def test_quiz_renders_stored_order_deterministically():
    """The server response must be byte-identical across renders, because the post
    page's ETag hashes `post.sections`: a server-side shuffle disagreed with its own
    validator, and every 304 served the first ordering forever. Randomisation moved
    to app.js (shuffleChoices), per page VIEW — see the template comment."""
    renders = {_render([MULTI_QUIZ]) for _ in range(20)}
    assert len(renders) == 1
    for answer_text, choices in _quiz_answers(next(iter(renders))):
        # data-answer indexes the STORED choices, which is what app.js relies on
        # when it permutes the buttons without recomputing anything.
        assert answer_text in ("Rayleigh scattering", "Longer path length")
    assert _quiz_answers(next(iter(renders)))[0][1] == ["Rayleigh scattering", "Ozone"]


def test_rendering_leaves_the_stored_section_untouched():
    """Storage is canonical: rendering must not mutate the section dict the caller
    passed (it is the ORM row's JSONB value)."""
    section = copy.deepcopy(MULTI_QUIZ)
    for _ in range(20):
        _render([section])
    assert section == MULTI_QUIZ


def test_malformed_quiz_question_renders_without_raising():
    """A hand-edited row missing `choices`/`answer_index` must degrade, not 500 the
    article page. `data-answer` comes out empty, which app.js reads as 'inert'
    rather than scoring every click as wrong."""
    html = _render([{"type": "quiz", "questions": [{"question": "orphan?"}]}])
    assert 'data-answer=""' in html
    assert 'class="quiz-choice"' not in html  # no buttons, so nothing to click


def test_video_without_embeddable_url_falls_back_to_link():
    html = _render([{"type": "video", "url": "https://example.org/raw.mp4",
                     "caption": "c", "attribution": "X", "source_url": "https://x.org"}])
    assert "<iframe" not in html
    assert 'href="https://example.org/raw.mp4"' in html
