"""Section-type schemas (spec §6): every type parses through the discriminated
union, and per-type invariants (quiz answer index, the mandatory quiz section) hold —
malformed sections must fail validation at the LLM boundary, never reach the
renderer."""

import pytest
from pydantic import TypeAdapter, ValidationError

from episteme.llm.schemas import PostDraft, QAReview, Section

section_adapter = TypeAdapter(Section)

QUIZ_SECTION = {
    "type": "quiz",
    "questions": [
        {"question": "q?", "choices": ["a", "b", "c"], "answer_index": 2,
         "explanation": "because"},
        {"question": "q2?", "choices": ["x", "y"], "answer_index": 0,
         "explanation": "also because"},
    ],
}

CHART_SECTION = {
    "type": "chart",
    "spec": {
        "mark": "bar",
        "data": {"values": [{"city": "Boston", "pct": 16.0},
                            {"city": "Chicago", "pct": 0.1}]},
        "encoding": {"x": {"field": "city", "type": "nominal"},
                     "y": {"field": "pct", "type": "quantitative"}},
        "title": "Obscuration",
    },
    "caption": "How much of the Sun each city loses.",
}

VALID_SECTIONS = [
    {"type": "prose", "text": "body"},
    {"type": "key_points", "items": ["a", "b"]},
    {"type": "image", "url": "https://x.org/i.jpg", "caption": "c"},
    {"type": "video", "url": "https://youtu.be/abc12345", "caption": ""},
    QUIZ_SECTION,
    CHART_SECTION,
    {"type": "diagram", "mermaid": "flowchart LR\n  A --> B"},
    {"type": "timeline", "events": [{"date": "1969", "label": "Apollo 11"},
                                    {"date": "2026", "label": "Artemis"}]},
    {"type": "glossary", "terms": [{"term": "quasar", "definition": "an AGN"}]},
]


def _draft(sections):
    return {
        "title": "t",
        "summary": "s",
        "difficulty": "intermediate",
        "topics": ["astronomy"],
        "sections": sections,
        "further_reading_urls": [],
    }


@pytest.mark.parametrize("raw", VALID_SECTIONS, ids=lambda s: s["type"])
def test_every_section_type_parses(raw):
    parsed = section_adapter.validate_python(raw)
    assert parsed.type == raw["type"]


# --- chart and diagram: the two types the browser draws ---------------------------
# Every other section renders from its JSON through a Jinja branch. These two hold a
# spec the browser executes, so a well-formed section can still put a blank box on
# the page — which is exactly what post 427 did, at quality_score 9, for two weeks.


def test_chart_spec_rejects_the_shape_that_shipped_a_blank_box():
    """Post 427's stored spec, verbatim. `spec` was `dict[str, Any]` — the one field
    in the whole union that escaped the grammar — so the model invented `x_axis` /
    `y_axis`, gave `data` a bare list, and omitted `mark` and `encoding` entirely.
    Vega-Lite rejected it and the figure collapsed to its empty caption. Against a
    typed spec that JSON cannot even be generated."""
    with pytest.raises(ValidationError):
        section_adapter.validate_python({
            "type": "chart",
            "spec": {
                "data": [{"city": "Fairbanks, AK", "obscuration": 36.8},
                         {"city": "Boston, MA", "obscuration": 16.0}],
                "title": "Partial Eclipse Obscuration for Selected Cities",
                "x_axis": "City",
                "y_axis": "% Obscuration",
            },
        })


def test_chart_encoding_must_name_fields_the_rows_actually_have():
    """The failure a shape constraint cannot catch: flawless Vega-Lite that draws an
    empty frame because the channel points at a key no row carries."""
    spec = dict(CHART_SECTION["spec"],
                encoding={"x": {"field": "town", "type": "nominal"},
                          "y": {"field": "pct", "type": "quantitative"}})
    with pytest.raises(ValidationError, match="in no row of data.values"):
        section_adapter.validate_python(dict(CHART_SECTION, spec=spec))


def test_chart_encoding_must_name_fields_EVERY_row_has():
    """A key only some rows carry passes a union check and draws one bar out of N —
    the same silent half-failure, harder to see than an empty frame."""
    spec = dict(
        CHART_SECTION["spec"],
        data={"values": [{"city": "Boston", "pct": 16.0}, {"city": "Chicago"}]},
    )
    with pytest.raises(ValidationError, match="in only some rows"):
        section_adapter.validate_python(dict(CHART_SECTION, spec=spec))


def test_chart_optional_colour_channel_is_checked_too():
    spec = dict(CHART_SECTION["spec"],
                encoding={**CHART_SECTION["spec"]["encoding"],
                          "color": {"field": "nope", "type": "nominal"}})
    with pytest.raises(ValidationError, match="encoding.color"):
        section_adapter.validate_python(dict(CHART_SECTION, spec=spec))
    ok = dict(CHART_SECTION["spec"],
              encoding={**CHART_SECTION["spec"]["encoding"],
                        "color": {"field": "city", "type": "nominal"}})
    assert section_adapter.validate_python(dict(CHART_SECTION, spec=ok))


def test_diagram_must_declare_a_mermaid_diagram_type():
    """Mermaid picks its parser from the opening keyword; without one the source is a
    syntax error and the figure renders blank. A DSL in a string is the one thing the
    grammar cannot constrain, so this is the cheap structural half."""
    with pytest.raises(ValidationError, match="not a diagram type"):
        section_adapter.validate_python(
            {"type": "diagram", "mermaid": "A --> B\nB --> C"}
        )


def test_diagram_allows_directives_and_frontmatter_before_the_declaration():
    parsed = section_adapter.validate_python({
        "type": "diagram",
        "mermaid": "%%{init: {'theme':'dark'}}%%\nsequenceDiagram\n  A->>B: hi",
    })
    assert parsed.type == "diagram"


def test_diagram_skips_the_frontmatter_BODY_not_only_its_fences():
    """Frontmatter is a YAML block whose body is arbitrary keys. Skipping only the
    `---` lines left `title:` reading as the declaration, so valid Mermaid was
    rejected and burned a repair retry."""
    parsed = section_adapter.validate_python({
        "type": "diagram",
        "mermaid": "---\ntitle: Formation sequence\nconfig:\n  theme: dark\n---\n"
                   "flowchart LR\n  A[Cloud] --> B[Star]",
    })
    assert parsed.type == "diagram"


def test_diagram_still_rejects_a_missing_declaration_after_frontmatter():
    with pytest.raises(ValidationError, match="not a diagram type"):
        section_adapter.validate_python(
            {"type": "diagram", "mermaid": "---\ntitle: X\n---\nA --> B"}
        )


def test_diagram_rejects_unclosed_frontmatter():
    with pytest.raises(ValidationError, match="never closed"):
        section_adapter.validate_python(
            {"type": "diagram", "mermaid": "---\ntitle: X\nflowchart LR\n  A --> B"}
        )


def test_quiz_holds_several_questions():
    parsed = section_adapter.validate_python(QUIZ_SECTION)
    assert [q.question for q in parsed.questions] == ["q?", "q2?"]


def test_quiz_answer_index_must_point_into_choices():
    with pytest.raises(ValidationError):
        section_adapter.validate_python(
            {"type": "quiz", "questions": [
                {"question": "q?", "choices": ["a", "b"], "answer_index": 2,
                 "explanation": "e"}]}
        )


def test_quiz_needs_at_least_one_question():
    with pytest.raises(ValidationError):
        section_adapter.validate_python({"type": "quiz", "questions": []})


def test_post_draft_without_quiz_is_rejected():
    """The grammar can't require a list *member*, so this validator is the only
    thing making "every post has a quiz" true — see schemas._require_quiz."""
    with pytest.raises(ValidationError, match="quiz"):
        PostDraft.model_validate(_draft([{"type": "prose", "text": "body"}]))


def test_qa_review_is_a_verdict_only():
    """QA edits through tools, so the verdict carries no body. When it did, a
    `demote` that happened to include sections failed validation, `request_validated`
    returned None, and the demotion was silently discarded — leaving the post
    unscored and queued for review forever."""
    for verdict in ("approve", "revise", "demote"):
        review = QAReview.model_validate(
            {"verdict": verdict, "quality_score": 6.0, "critique": "c",
             "revised_sections": [{"type": "prose", "text": "body"}]}
        )
        assert review.verdict == verdict
        assert not hasattr(review, "revised_sections")


def test_unknown_section_type_rejected():
    with pytest.raises(ValidationError):
        section_adapter.validate_python({"type": "carousel", "items": []})


def test_post_draft_accepts_mixed_section_palette():
    draft = PostDraft.model_validate(_draft(VALID_SECTIONS))
    assert [s.type for s in draft.sections] == [s["type"] for s in VALID_SECTIONS]
