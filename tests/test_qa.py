"""QA stage: revision application (DB-built sections must survive), the review
schema, and screenshot stripping in the observability layer."""

from episteme.llm.observe import _hash_messages, _strip_images
from episteme.llm.schemas import QAReview

_SECTIONS = [
    {"type": "prose", "text": "old body"},
    {"type": "key_points", "items": ["a", "b"]},
    {"type": "sources", "items": [{"title": "t", "url": "u", "outlet": "o"}]},
    {"type": "further_reading", "items": [{"title": "f", "url": "v", "outlet": "w"}]},
]


def test_apply_revision_replaces_body_and_keeps_db_tail():
    from episteme.worker.qa import apply_revision

    review = QAReview.model_validate(
        {
            "verdict": "revise",
            "quality_score": 6.0,
            "critique": "verbatim summary",
            "revised_sections": [{"type": "prose", "text": "new body"}],
        }
    )
    result = apply_revision(_SECTIONS, review)
    assert result[0] == {"type": "prose", "text": "new body"}
    # DB-built citations survive untouched, in order, at the tail.
    assert result[-2]["type"] == "sources"
    assert result[-1]["type"] == "further_reading"
    assert len(result) == 3


def test_apply_revision_without_sections_keeps_existing_body():
    from episteme.worker.qa import apply_revision

    review = QAReview.model_validate(
        {"verdict": "revise", "quality_score": 5.0, "critique": "title only",
         "revised_title": "Better title"}
    )
    result = apply_revision(_SECTIONS, review)
    assert result == _SECTIONS


def test_qa_review_schema_rejects_unknown_verdict():
    import pytest

    with pytest.raises(Exception):
        QAReview.model_validate({"verdict": "maybe", "quality_score": 5, "critique": "c"})


def _image_message(payload: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
        ],
    }


def test_strip_images_replaces_payload_and_keeps_text():
    stripped = _strip_images([_image_message("A" * 100_000)])
    parts = stripped[0]["content"]
    assert parts[0] == {"type": "text", "text": "look at this"}
    url = parts[1]["image_url"]["url"]
    assert "stripped" in url and len(url) < 100


def test_strip_images_is_deterministic_for_chain_hashing():
    """The prefix-hash integrity check hashes stripped messages; re-sending the
    same history (with the same inline image) must hash identically."""
    messages = [{"role": "system", "content": "s"}, _image_message("B" * 5000)]
    assert _hash_messages(_strip_images(messages)) == _hash_messages(_strip_images(messages))


def test_strip_images_leaves_plain_messages_untouched():
    messages = [{"role": "user", "content": "plain text"}]
    assert _strip_images(messages) == messages
