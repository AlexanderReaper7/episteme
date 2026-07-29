"""Render smoke test: post.html must render every section type without error and
with the right markers — a template typo here would otherwise only surface on a
live post page (or a QA screenshot) at 3 AM."""

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
    {"type": "quiz", "question": "Why is the sky blue?",
     "choices": ["Rayleigh scattering", "Ozone"], "answer_index": 0,
     "explanation": "Shorter wavelengths scatter more."},
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
        id=1, kind="feature", title="T", summary="S", difficulty="intermediate",
        topics=["astronomy"], sections=sections, reading_time_minutes=3,
        generated_at=datetime(2026, 7, 19, tzinfo=UTC), model_used="test-model",
    )
    return templates.env.get_template("post.html").render(post=post)


def test_all_section_types_render():
    html = _render(ALL_SECTIONS)
    assert "<strong>bold</strong>" in html                                # prose markdown
    assert "point one" in html                                            # key_points
    assert 'src="https://cdn.example.org/webb.jpg"' in html               # image hotlink
    assert "ESA Webb" in html                                             # stamped attribution
    assert 'src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in html  # whitelist embed
    assert 'data-answer="0"' in html and "Rayleigh scattering" in html    # quiz
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

    with_quiz = _render([{"type": "quiz", "question": "q", "choices": ["a", "b"],
                          "answer_index": 1, "explanation": "e"}])
    assert "section-quiz" in with_quiz and 'data-answer="1"' in with_quiz

    prose_only = _render([{"type": "prose", "text": "just text"}])
    assert "section-chart" not in prose_only and "section-quiz" not in prose_only
    # No eager renderer script tags either (the fingerprint map may still name them).
    assert 'src="/static/vendor/vega' not in prose_only
    assert 'src="/static/vendor/mermaid.min.js' not in prose_only


def test_video_without_embeddable_url_falls_back_to_link():
    html = _render([{"type": "video", "url": "https://example.org/raw.mp4",
                     "caption": "c", "attribution": "X", "source_url": "https://x.org"}])
    assert "<iframe" not in html
    assert 'href="https://example.org/raw.mp4"' in html
