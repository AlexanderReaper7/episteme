import importlib
import json

import httpx

from episteme.llm.gateway import LLMError, LLMGateway
from episteme.llm.observe import (
    _attempt_id,
    _chain_info,
    _stage,
    _story_id,
    llm_context,
    llm_conversation,
    record_llm_call,
)
from episteme.llm.schemas import TriageResult

# `episteme.llm.gateway` as a dotted monkeypatch target resolves to the singleton
# *instance* re-exported by llm/__init__, so patch the module object instead.
gateway_module = importlib.import_module("episteme.llm.gateway")


def test_llm_context_nesting_composes():
    assert _stage.get() is None and _story_id.get() is None
    with llm_context(stage="write", story_id=5, attempt_id="att1"):
        assert _stage.get() == "write" and _story_id.get() == 5
        assert _attempt_id.get() == "att1"
        # Nested override changes only the field passed; story_id survives.
        with llm_context(stage="research"):
            assert _stage.get() == "research" and _story_id.get() == 5
            assert _attempt_id.get() == "att1"  # the attempt spans nested stages
        assert _stage.get() == "write"
    assert _stage.get() is None and _story_id.get() is None and _attempt_id.get() is None


async def test_record_is_noop_when_disabled():
    # conftest disables llm_log_enabled; must not touch the DB or raise.
    await record_llm_call(role="fast", model="m", kind="chat", duration_ms=1)


def test_chain_info_outside_conversation_stores_full():
    messages = [{"role": "user", "content": "u"}]
    chain_id, seq, delta = _chain_info(messages, {"role": "assistant", "content": "a"})
    assert chain_id is None and seq is None
    assert delta == messages


def test_chain_info_stores_deltas_and_reconstructs():
    """Simulate an agent loop: each call's stored delta + response must
    concatenate back to the exact full transcript."""
    system = {"role": "system", "content": "s"}
    user = {"role": "user", "content": "seed"}
    asst1 = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
    tool1 = {"role": "tool", "tool_call_id": "c1", "content": "result"}
    asst2 = {"role": "assistant", "content": "done"}

    with llm_conversation():
        # Turn 1: request [s, u] -> asst1
        cid1, seq1, delta1 = _chain_info([system, user], asst1)
        # Turn 2: loop appended asst1 + tool result -> request grows -> asst2
        cid2, seq2, delta2 = _chain_info([system, user, asst1, tool1], asst2)

    assert cid1 == cid2 and cid1 is not None
    assert (seq1, seq2) == (0, 1)
    assert delta1 == [system, user]
    assert delta2 == [tool1]  # asst1 lives in row 1's response column, not row 2
    reconstructed = delta1 + [asst1] + delta2 + [asst2]
    assert reconstructed == [system, user, asst1, tool1, asst2]


def test_chain_info_detects_rewritten_history():
    system = {"role": "system", "content": "s"}
    user = {"role": "user", "content": "seed"}
    asst = {"role": "assistant", "content": "a"}

    with llm_conversation():
        cid1, _, _ = _chain_info([system, user], asst)
        # History mutated (system prompt swapped) — prefix hash mismatch.
        mutated = [{"role": "system", "content": "DIFFERENT"}, user, asst]
        cid2, seq2, delta2 = _chain_info(mutated, None)

    assert cid2 != cid1
    assert seq2 == 0
    assert delta2 == mutated  # falls back to storing the full transcript


def test_separate_conversations_get_separate_chains():
    msgs = [{"role": "user", "content": "u"}]
    with llm_conversation():
        cid1, _, _ = _chain_info(msgs, None)
    with llm_conversation():
        cid2, _, _ = _chain_info(msgs, None)
    assert cid1 != cid2


def _completion(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


async def test_gateway_records_each_call_including_retries(monkeypatch):
    records: list[dict] = []

    async def fake_record(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(gateway_module, "record_llm_call", fake_record)

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=_completion('{"decision": "nope"}'))
        return httpx.Response(
            200,
            json=_completion(
                '{"decision": "skip", "quality_score": 1.0, "topics": ["x"], "reason": "r"}'
            ),
        )

    gw = LLMGateway(transport=httpx.MockTransport(handler))
    await gw.complete_json("fast", "sys", "user", TriageResult)

    # One record per HTTP call — the repair retry is its own row.
    assert len(records) == 2
    assert all(r["kind"] == "chat" for r in records)
    assert records[0]["prompt_tokens"] == 10
    assert records[0]["request"]["constrained"] is True
    assert "decision" in records[1]["response"]["content"]
    # The retry's request must carry the repair prompt.
    assert "failed validation" in records[1]["request"]["messages"][1]["content"]


async def test_gateway_records_errors(monkeypatch):
    records: list[dict] = []

    async def fake_record(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(gateway_module, "record_llm_call", fake_record)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    gw = LLMGateway(transport=httpx.MockTransport(handler))
    # LLMError, not the underlying httpx.HTTPStatusError: the gateway wraps
    # transport failures so callers' `except LLMError` fallbacks cover them.
    try:
        await gw.chat("fast", "sys", "user")
    except LLMError:
        pass
    assert len(records) == 1
    assert records[0]["error"]
    assert records[0].get("response") is None


async def test_tool_chat_records_full_message(monkeypatch):
    records: list[dict] = []

    async def fake_record(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(gateway_module, "record_llm_call", fake_record)

    tool_call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "web_search", "arguments": json.dumps({"query": "q"})},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": None, "tool_calls": [tool_call]}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    gw = LLMGateway(transport=httpx.MockTransport(handler))
    message = await gw.chat_messages("fast", [{"role": "user", "content": "hi"}], tools=[{}])
    assert message["tool_calls"] == [tool_call]
    assert records[0]["kind"] == "tool-chat"
    assert records[0]["response"]["tool_calls"] == [tool_call]
