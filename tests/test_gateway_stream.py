"""What this pins: a streamed turn and a blocking one produce the SAME message.

`run_tool_loop`, `observe`, and every caller downstream read the assistant message
without knowing how it arrived. That only holds if reassembly is exact, and the
place it can silently stop being exact is tool calls: arguments arrive as string
fragments keyed by `index`, and only the first fragment carries `id` and `name`.
Concatenating by arrival order instead of by index interleaves two parallel calls
into one unparseable JSON string — which fails far away, as a tool that "did not
get its arguments", not here.
"""

import httpx
import pytest

from episteme.llm.gateway import LLMGateway, LLMError, StreamAccumulator


def _chunk(delta: dict, finish: str | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _call(index: int, *, id: str = "", name: str = "", args: str = "") -> dict:
    call: dict = {"index": index, "function": {}}
    if id:
        call["id"] = id
    if name:
        call["function"]["name"] = name
    if args:
        call["function"]["arguments"] = args
    return call


def _body(chunks: list[dict]) -> bytes:
    import json

    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


# --- the accumulator, no HTTP ------------------------------------------------


def test_text_deltas_concatenate_in_order():
    acc = StreamAccumulator()
    assert [acc.feed(_chunk({"content": part})) for part in ("Hel", "lo ", "world")] == [
        "Hel",
        "lo ",
        "world",
    ]
    assert acc.message() == {"role": "assistant", "content": "Hello world"}


def test_tool_call_arguments_concatenate_per_index():
    """The core case: one call's JSON arrives in four pieces, and only the first
    carries the id and the name."""
    acc = StreamAccumulator()
    acc.feed(_chunk({"tool_calls": [_call(0, id="c1", name="fetch_page", args='{"ur')]}))
    acc.feed(_chunk({"tool_calls": [_call(0, args='l": "https://x')]}))
    acc.feed(_chunk({"tool_calls": [_call(0, args='.test/a"')]}))
    acc.feed(_chunk({"tool_calls": [_call(0, args="}")]}, finish="tool_calls"))
    assert acc.message() == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "fetch_page", "arguments": '{"url": "https://x.test/a"}'},
            }
        ],
    }


def test_two_parallel_tool_calls_do_not_interleave():
    """The failure mode that motivates keying by index: chunks for call 0 and call 1
    arrive interleaved, and appending by arrival would splice them together."""
    acc = StreamAccumulator()
    acc.feed(_chunk({"tool_calls": [_call(0, id="a", name="search_posts", args='{"q":')]}))
    acc.feed(_chunk({"tool_calls": [_call(1, id="b", name="get_post", args='{"post_')]}))
    acc.feed(_chunk({"tool_calls": [_call(0, args=' "voyager"}')]}))
    acc.feed(_chunk({"tool_calls": [_call(1, args='id": 12}')]}))
    calls = acc.message()["tool_calls"]
    assert [c["id"] for c in calls] == ["a", "b"]
    assert calls[0]["function"]["arguments"] == '{"q": "voyager"}'
    assert calls[1]["function"]["arguments"] == '{"post_id": 12}'


def test_null_argument_fragments_and_empty_deltas_are_survivable():
    acc = StreamAccumulator()
    acc.feed(_chunk({"role": "assistant"}))  # opening chunk, no content
    acc.feed(_chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "x"}}]}))
    acc.feed(_chunk({"tool_calls": [{"index": 0, "function": {"arguments": None}}]}))
    acc.feed(_chunk({"content": None}))
    acc.feed({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    assert acc.usage == {"prompt_tokens": 7, "completion_tokens": 3}
    assert acc.message()["tool_calls"][0]["function"] == {"name": "x", "arguments": ""}


def test_content_is_none_not_empty_string_when_only_tool_calls():
    """Matches what a non-streaming server sends. An empty string would be a
    different message from the blocking path's, which is the whole thing this
    class exists to prevent."""
    acc = StreamAccumulator()
    acc.feed(_chunk({"tool_calls": [_call(0, id="c", name="n", args="{}")]}))
    assert acc.message()["content"] is None


# --- the endpoint ------------------------------------------------------------


async def test_stream_yields_text_then_one_terminal_message():
    chunks = [_chunk({"content": "a"}), _chunk({"content": "b"}, finish="stop")]
    gateway = LLMGateway(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=_body(chunks)))
    )
    events = [e async for e in gateway.chat_stream("chat", [{"role": "user", "content": "hi"}])]
    assert events[:-1] == [{"type": "text", "delta": "a"}, {"type": "text", "delta": "b"}]
    assert events[-1] == {"type": "message", "message": {"role": "assistant", "content": "ab"}}


async def test_stream_request_asks_for_streaming_and_usage():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_body([_chunk({"content": "x"})]))

    gateway = LLMGateway(transport=httpx.MockTransport(handler))
    async for _ in gateway.chat_stream("chat", [{"role": "user", "content": "hi"}], tools=[{"t": 1}]):
        pass
    assert seen["stream"] is True
    assert seen["stream_options"] == {"include_usage": True}
    assert seen["tool_choice"] == "auto"


async def test_a_non_200_reaches_the_caller_as_llmerror():
    gateway = LLMGateway(
        transport=httpx.MockTransport(lambda r: httpx.Response(503, text="model loading"))
    )
    with pytest.raises(LLMError):
        async for _ in gateway.chat_stream("chat", [{"role": "user", "content": "hi"}]):
            pass


async def test_a_stream_that_dies_midway_reaches_the_caller_as_llmerror():
    """Half a message is not a message. The partial text already yielded is the
    caller's to discard; what must not happen is a raw JSONDecodeError blowing
    through every `except LLMError` in the codebase."""
    truncated = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\ndata: {"choi\n\n'
    gateway = LLMGateway(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=truncated))
    )
    seen = []
    with pytest.raises(LLMError):
        async for event in gateway.chat_stream("chat", [{"role": "user", "content": "hi"}]):
            seen.append(event)
    assert seen == [{"type": "text", "delta": "partial"}]
