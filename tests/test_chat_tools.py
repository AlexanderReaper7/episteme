"""The permission property, checked mechanically over the whole table.

The user's requirement is one sentence: the assistant must ask before anything
that can write. That is a property of every present and future entry in
`chat_tools.TOOLS`, so it is tested by iterating the table rather than by naming
`write_article_from_url` - a test that names the tool passes forever and stops
covering the thing it was written for the moment a second write tool is added.

`tripwire` is the shape that makes "did not execute" observable: a handler that
raises if it is ever awaited. Asserting that `dispatch` raises `WriteProposed` on
its own would pass just as well if the handler had run first and the exception
came afterwards.
"""

import dataclasses
from types import SimpleNamespace

import pytest

from episteme.config import settings
from episteme.llm import chat, chat_tools
from episteme.llm.chat_tools import (
    ChatContext,
    ChatTool,
    RegistryError,
    ToolRefused,
    WriteProposed,
    dispatch,
    execute_approved,
    parse_args,
    validate_registry,
)

WRITE_TOOLS = [tool for tool in chat_tools.TOOLS.values() if tool.writes]
READ_TOOLS = [tool for tool in chat_tools.TOOLS.values() if not tool.writes]


def tripwire(tool: ChatTool) -> ChatTool:
    """The same tool with a handler that cannot be run without being noticed."""

    async def _explode(ctx, args):
        raise AssertionError(f"{tool.name} executed without approval")

    return dataclasses.replace(tool, handler=_explode)


def test_there_is_at_least_one_write_tool_to_check():
    """Guards the guard: if the table ever holds only read tools, every test below
    passes vacuously and the gate is untested rather than intact."""
    assert WRITE_TOOLS


@pytest.mark.parametrize("tool", WRITE_TOOLS, ids=lambda t: t.name)
async def test_a_write_tool_is_never_executed_by_dispatch(tool, monkeypatch):
    monkeypatch.setitem(chat_tools.TOOLS, tool.name, tripwire(tool))
    with pytest.raises(WriteProposed) as raised:
        await dispatch(ChatContext(session=None), tool.name, {"url": "https://example.org"})
    assert raised.value.tool.name == tool.name
    assert raised.value.tool_args == {"url": "https://example.org"}


@pytest.mark.parametrize("tool", WRITE_TOOLS, ids=lambda t: t.name)
async def test_a_write_tool_carries_a_sentence_for_the_approval_card(tool):
    """A card asking you to approve a blank is a card you approve. The description
    has to come from the ARGUMENTS, so it also has to mention them."""
    assert tool.describe is not None
    sentence = tool.describe({"url": "https://nature.com/articles/x"})
    assert "nature.com/articles/x" in sentence


@pytest.mark.parametrize("tool", WRITE_TOOLS, ids=lambda t: t.name)
async def test_approval_is_the_path_that_does_execute(tool, monkeypatch):
    """The other half of the property. If nothing runs after approval either, the
    gate is not a gate, it is a wall, and these tests would not tell them apart."""
    ran: list[dict] = []

    async def _run(ctx, args):
        ran.append(args)
        return "done"

    monkeypatch.setitem(chat_tools.TOOLS, tool.name, dataclasses.replace(tool, handler=_run))
    result = await execute_approved(ChatContext(session=None), tool.name, {"url": "u"})
    assert result == "done"
    assert ran == [{"url": "u"}]


def test_the_registry_rejects_a_write_tool_with_no_description():
    """Enforced at import, so a malformed table is a startup failure rather than a
    card that renders empty three weeks later."""
    broken = dataclasses.replace(WRITE_TOOLS[0], describe=None)
    with pytest.raises(RegistryError, match="describe"):
        validate_registry({broken.name: broken})


def test_the_registry_rejects_a_key_that_disagrees_with_the_tool_name():
    """The model calls tools by the name in the schema; dispatch looks them up by
    the dict key. A disagreement is a tool that can be advertised and not found."""
    tool = READ_TOOLS[0]
    with pytest.raises(RegistryError):
        validate_registry({"something_else": tool})


def test_the_shipped_registry_is_valid():
    validate_registry(chat_tools.TOOLS)


async def test_a_budget_refusal_is_not_an_error(monkeypatch):
    """Refusals feed `run_tool_loop`'s stuck-loop breaker, so they must arrive as
    `ToolRefused` with something the model can act on, not as a crash."""
    monkeypatch.setattr(settings, "chat_max_searches", 0)
    with pytest.raises(ToolRefused, match="budget"):
        await dispatch(ChatContext(session=None), "web_search", {"query": "x"})


async def test_an_unknown_tool_is_answered_not_raised():
    """Models invent tool names. That is a sentence back to the model, not a 500."""
    assert "Unknown tool" in await dispatch(ChatContext(session=None), "rm_rf", {})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"url": "x"}', {"url": "x"}),
        ({"url": "x"}, {"url": "x"}),
        ("", {}),
        ("not json", {}),
        ("[1, 2]", {}),  # valid JSON, wrong shape - handlers index it as a dict
    ],
)
def test_tool_arguments_reach_handlers_as_a_dict(raw, expected):
    assert parse_args(raw) == expected


def test_every_tool_is_advertised_to_the_model():
    """Including the write tools: withholding them would leave the model unable to
    say what it wants to do, and it is the EXECUTION that is gated, not the ask."""
    advertised = {schema["function"]["name"] for schema in chat_tools.schemas()}
    assert advertised == set(chat_tools.TOOLS)


# --- the prompt's shape ------------------------------------------------------


def _row(role, content="x", **kw):
    return SimpleNamespace(
        id=kw.pop("id", 1), role=role, content=content,
        tool_name=kw.pop("tool_name", None), tool_call_id=kw.pop("tool_call_id", None),
        proposal_status=kw.pop("proposal_status", None), **kw,
    )


@pytest.mark.parametrize(
    "header,tail",
    [
        (None, None),
        ("The reader currently has this article open.", [{"role": "user", "content": "hi"}]),
        (None, [{"role": "assistant", "content": None, "tool_calls": []},
                {"role": "tool", "tool_call_id": "c1", "content": "done"}]),
    ],
)
def test_the_prompt_carries_exactly_one_system_message_and_it_is_first(header, tail):
    """Found live, 2026-08-12: the open-article header was appended as a SECOND
    system message, and Qwopus's chat template raises `System message must be at
    the beginning` on anything system-role that is not `loop.first`. llama-server
    reports that as a bare 400 naming no message, so the only cheap way to keep
    it fixed is to assert the shape here."""
    rows = [_row("user"), _row("assistant"), _row("proposal", tool_name="write_article_from_url",
                                                  proposal_status="approved")]
    messages = chat.build_messages(rows, header=header, tail=tail)
    systems = [i for i, m in enumerate(messages) if m["role"] == "system"]
    assert systems == [0]
    if header:
        assert header in messages[0]["content"]
        assert chat.SYSTEM in messages[0]["content"]
