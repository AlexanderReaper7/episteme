import json
import math

import httpx
import pytest

from episteme.llm.gateway import LLMError, LLMGateway, _truncate_normalize, grammar_safe
from episteme.llm.schemas import PostDraft, QAReview, TriageResult
from episteme.models import EMBEDDING_DIM


def _completion(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _gateway_with(handler) -> LLMGateway:
    return LLMGateway(transport=httpx.MockTransport(handler))


async def test_complete_json_valid():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json=_completion(
                '{"decision": "write", "quality_score": 7.5, "topics": ["astronomy"], "reason": "solid research"}'
            ),
        )

    result = await _gateway_with(handler).complete_json("fast", "sys", "user", TriageResult)
    assert result.decision == "write"
    assert result.topics == ["astronomy"]


async def test_complete_json_retries_on_invalid_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=_completion('{"decision": "maybe?"}'))
        body = json.loads(request.content)
        # The repair prompt must include the validation error.
        assert "failed validation" in body["messages"][1]["content"]
        return httpx.Response(
            200,
            json=_completion('{"decision": "skip", "quality_score": 0.5, "topics": ["noise"], "reason": "spam"}'),
        )

    result = await _gateway_with(handler).complete_json("fast", "sys", "user", TriageResult)
    assert calls == 2
    assert result.decision == "skip"


async def test_complete_json_gives_up_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("not json at all"))

    with pytest.raises(LLMError):
        await _gateway_with(handler).complete_json("fast", "sys", "user", TriageResult)


async def test_complete_json_falls_back_to_json_object_format():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["response_format"]["type"])
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "unknown response_format"})
        return httpx.Response(
            200,
            json=_completion('{"decision": "aggregate", "quality_score": 3, "topics": ["tech"], "reason": "ok"}'),
        )

    result = await _gateway_with(handler).complete_json("fast", "sys", "user", TriageResult)
    assert calls == ["json_schema", "json_object"]
    assert result.decision == "aggregate"


@pytest.mark.parametrize(
    "raised",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("connection refused"),
    ],
)
async def test_transport_failures_reach_callers_as_llm_error(raised):
    """Regression (found live, 2026-07-29): the router had to swap the fast model
    in, the request sat 600s without a byte, and the raw `httpx.ReadTimeout` blew
    straight through `_name_clusters`' `except LLMError` fallback — destroying a
    job that had already spent ten minutes embedding and clustering.

    Every caller writes its degradation against `LLMError` because the gateway is
    the single choke point; a transport error must not be the one failure mode
    that bypasses all of them."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    with pytest.raises(LLMError):
        await _gateway_with(handler).chat("fast", "sys", "user")


async def test_http_status_errors_are_llm_errors_too():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model loading")

    with pytest.raises(LLMError):
        await _gateway_with(handler).chat("fast", "sys", "user")


@pytest.mark.parametrize(
    "body",
    [
        {"text": "<html>gateway timeout</html>"},          # 200, not JSON at all
        {"json": {"error": {"message": "no slot available"}}},  # JSON, no choices
        {"json": {"choices": []}},                          # choices, but empty
        {"json": {"choices": [{"message": {}}]}},           # message, no content
    ],
    ids=["not-json", "no-choices", "empty-choices", "no-content"],
)
async def test_a_malformed_200_is_an_llm_error_like_any_other_failure(body):
    """The other half of the same contract. A response that arrives but cannot be
    read is not a different kind of problem from one that never arrives — but it
    used to be a differently-typed one, because the parse sat outside the guarded
    region and escaped as JSONDecodeError / KeyError / IndexError, past every
    `except LLMError` fallback in the codebase.

    llama-server produces all four of these: a proxy's own error page, an error
    object with a 200, a truncated stream, a message with no content."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, **body)

    with pytest.raises(LLMError):
        await _gateway_with(handler).chat("fast", "sys", "user")


async def test_a_malformed_200_is_an_llm_error_on_the_tool_path_too():
    """The writer's agentic loop uses `chat_messages`, not `chat` — the same
    parse, so it needs the same guard, or one stray body fails the whole run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "no slot available"})

    with pytest.raises(LLMError):
        await _gateway_with(handler).chat_messages("main", [{"role": "user", "content": "x"}])


async def test_a_malformed_embedding_response_is_an_llm_error(monkeypatch):
    """`_embed_labels` degrades topic resolution to slug matching on LLMError; a
    KeyError here would instead fail whatever stage was resolving topics."""
    from episteme.config import settings

    monkeypatch.setattr(settings, "llm_model_embed", "test-embed")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})  # no "data"

    with pytest.raises(LLMError):
        await _gateway_with(handler).embed(["some text"])


async def test_embed_transport_failure_is_an_llm_error(monkeypatch):
    """The embed client is a second httpx client on a different port — it needs the
    same wrapping, not merely the chat path."""
    from episteme.config import settings

    monkeypatch.setattr(settings, "llm_model_embed", "test-embed")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    gateway = LLMGateway(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError):
        await gateway.embed(["some text"])


async def test_embed_truncates_and_normalizes(monkeypatch):
    from episteme.config import settings

    monkeypatch.setattr(settings, "llm_model_embed", "test-embed")

    def handler(request: httpx.Request) -> httpx.Response:
        vector = [1.0] * (EMBEDDING_DIM + 256)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector}]})

    vectors = await _gateway_with(handler).embed(["hello"])
    assert len(vectors[0]) == EMBEDDING_DIM
    norm = math.sqrt(sum(x * x for x in vectors[0]))
    assert abs(norm - 1.0) < 1e-9


async def test_embed_without_model_configured_raises(monkeypatch):
    from episteme.config import settings

    monkeypatch.setattr(settings, "llm_model_embed", "")
    with pytest.raises(LLMError, match="No model configured"):
        await _gateway_with(lambda r: httpx.Response(500)).embed(["hello"])


def test_truncate_normalize_rejects_short_vectors():
    with pytest.raises(LLMError, match="dims"):
        _truncate_normalize([1.0] * (EMBEDDING_DIM - 1))


def test_post_draft_schema_discriminated_union():
    draft = PostDraft.model_validate(
        {
            "title": "T",
            "summary": "S",
            "difficulty": "intermediate",
            "topics": ["physics"],
            "sections": [
                {"type": "prose", "text": "Body"},
                {"type": "key_points", "items": ["a", "b"]},
            ],
            "further_reading_urls": ["https://cern.ch/hilumi"],
        }
    )
    assert draft.sections[0].type == "prose"
    assert draft.further_reading_urls == ["https://cern.ch/hilumi"]
    # Required: an omitted list would be indistinguishable from "none qualify".
    with pytest.raises(Exception):
        PostDraft.model_validate(
            {
                "title": "T",
                "summary": "S",
                "difficulty": "intermediate",
                "topics": ["physics"],
                "sections": [{"type": "prose", "text": "Body"}],
            }
        )
    with pytest.raises(Exception):
        PostDraft.model_validate(
            {
                "title": "T",
                "summary": "S",
                "difficulty": "extreme",
                "topics": ["physics"],
                "sections": [{"type": "prose", "text": "Body"}],
            }
        )


def test_grammar_safe_drops_oversized_max_length():
    schema = QAReview.model_json_schema()
    safe = grammar_safe(schema)
    assert "maxLength" not in safe["properties"]["critique"]  # 2000 > grammar limit
    # Small caps and everything else survive untouched.
    assert TriageResult.model_json_schema() == grammar_safe(TriageResult.model_json_schema())
    assert schema["properties"]["critique"]["maxLength"] == 2000  # input not mutated


async def test_chat_sends_grammar_safe_schema():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent = body["response_format"]["json_schema"]["schema"]
        assert "maxLength" not in sent["properties"]["critique"]
        return httpx.Response(
            200,
            json=_completion('{"verdict": "approve", "quality_score": 8.0, "critique": "fine"}'),
        )

    result = await _gateway_with(handler).complete_json("main", "sys", "user", QAReview)
    assert result.verdict == "approve"
