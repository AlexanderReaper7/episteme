"""Section-type schemas (spec §6): every type parses through the discriminated
union, and per-type invariants (quiz answer index) hold — malformed sections must
fail validation at the LLM boundary, never reach the renderer."""

import pytest
from pydantic import TypeAdapter, ValidationError

from episteme.llm.schemas import PostDraft, Section

section_adapter = TypeAdapter(Section)

VALID_SECTIONS = [
    {"type": "prose", "text": "body"},
    {"type": "key_points", "items": ["a", "b"]},
    {"type": "image", "url": "https://x.org/i.jpg", "caption": "c"},
    {"type": "video", "url": "https://youtu.be/abc12345", "caption": ""},
    {"type": "quiz", "question": "q?", "choices": ["a", "b", "c"], "answer_index": 2,
     "explanation": "because"},
    {"type": "chart", "spec": {"mark": "bar", "data": {"values": [{"x": 1}]}}},
    {"type": "diagram", "mermaid": "flowchart LR; A --> B"},
    {"type": "timeline", "events": [{"date": "1969", "label": "Apollo 11"},
                                    {"date": "2026", "label": "Artemis"}]},
    {"type": "glossary", "terms": [{"term": "quasar", "definition": "an AGN"}]},
]


@pytest.mark.parametrize("raw", VALID_SECTIONS, ids=lambda s: s["type"])
def test_every_section_type_parses(raw):
    parsed = section_adapter.validate_python(raw)
    assert parsed.type == raw["type"]


def test_quiz_answer_index_must_point_into_choices():
    with pytest.raises(ValidationError):
        section_adapter.validate_python(
            {"type": "quiz", "question": "q?", "choices": ["a", "b"],
             "answer_index": 2, "explanation": "e"}
        )


def test_unknown_section_type_rejected():
    with pytest.raises(ValidationError):
        section_adapter.validate_python({"type": "carousel", "items": []})


def test_post_draft_accepts_mixed_section_palette():
    draft = PostDraft.model_validate(
        {
            "title": "t",
            "summary": "s",
            "difficulty": "intermediate",
            "topics": ["astronomy"],
            "sections": VALID_SECTIONS,
            "further_reading_urls": [],
        }
    )
    assert [s.type for s in draft.sections] == [s["type"] for s in VALID_SECTIONS]
