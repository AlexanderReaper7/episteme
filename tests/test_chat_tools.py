"""The permission property, checked mechanically over the whole table.

The user's requirement is one sentence: the assistant must ask before anything
that can write. That is a property of every present and future entry in
`llm.tools.TOOLS`, so it is tested by iterating the table rather than by naming
`write_article_from_url` - a test that names the tool passes forever and stops
covering the thing it was written for the moment a second write tool is added.

It iterates the WHOLE registry, not the assistant's slice of it. The gate lives
in `agent._dispatch`, above every handler for every harness, so a write tool
added to the writer or the QA reviewer is covered here by construction rather
than by somebody remembering to extend this file.

`tripwire` is what makes "did not execute" observable: a handler that raises if
it is ever awaited. Asserting that dispatch raises `WriteProposed` on its own
would pass just as well if the handler had run first and the exception came
afterwards.
"""

import dataclasses
from types import SimpleNamespace

import pytest

from episteme.config import settings
from episteme.llm import chat, tools
from episteme.llm.agent import _dispatch
from episteme.llm.chat_tools import CHAT_TOOL_NAMES, ChatContext, execute_approved
from episteme.llm.harness import HarnessError, build, fill
from episteme.llm.tools import RegistryError, Tool, ToolRefused, WriteProposed, parse_args

WRITE_TOOLS = [tool for tool in tools.TOOLS.values() if tool.writes]
READ_TOOLS = [tool for tool in tools.TOOLS.values() if not tool.writes]


def tripwire(tool: Tool) -> Tool:
    """The same tool with a handler that cannot be run without being noticed."""

    async def _explode(ctx, args):
        raise AssertionError(f"{tool.name} executed without approval")

    return dataclasses.replace(tool, handler=_explode)


def harness_offering(*names: str, context=None):
    """A minimal harness that offers exactly these tools. Built AFTER any
    monkeypatch of the registry, because `build` resolves names to objects."""
    return build(
        name="test",
        system="s",
        tools=list(names),
        role="chat",
        max_steps=4,
        wall_clock_seconds=30,
        context=context if context is not None else ChatContext(session=None),
    )


def test_there_is_at_least_one_write_tool_to_check():
    """Guards the guard: if the table ever holds only read tools, every test below
    passes vacuously and the gate is untested rather than intact."""
    assert WRITE_TOOLS


@pytest.mark.parametrize("tool", WRITE_TOOLS, ids=lambda t: t.name)
async def test_a_write_tool_is_never_executed_by_the_loop(tool, monkeypatch):
    monkeypatch.setitem(tools.TOOLS, tool.name, tripwire(tool))
    harness = harness_offering(tool.name)
    with pytest.raises(WriteProposed) as raised:
        await _dispatch(harness, tool.name, {"url": "https://example.org"})
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

    monkeypatch.setitem(tools.TOOLS, tool.name, dataclasses.replace(tool, handler=_run))
    result = await execute_approved(ChatContext(session=None), tool.name, {"url": "u"})
    assert result == "done"
    assert ran == [{"url": "u"}]


async def _noop(ctx, args):
    return "ok"


def a_tool(name="candidate", *, schema_name=None, **kw) -> Tool:
    """A fresh entry, with its OWN schema dict. Never `dataclasses.replace` on a
    shipped tool for this: the copy shares the original's schema, so renaming
    through it edits the schema of a tool the rest of the suite is still using."""
    return Tool(
        name=name,
        schema=tools.function(schema_name or name, "d", {}, []),
        handler=_noop,
        **kw,
    )


def test_the_registry_rejects_a_write_tool_with_no_description():
    """Enforced at import, so a malformed table is a startup failure rather than a
    card that renders empty three weeks later."""
    with pytest.raises(RegistryError, match="describe"):
        tools.register(a_tool(writes=True))


def test_the_registry_rejects_a_schema_that_disagrees_with_the_tool_name():
    """The model calls tools by the name in the schema; a harness looks them up by
    the registry key. A disagreement is a tool that can be advertised and not
    found."""
    with pytest.raises(RegistryError, match="disagrees"):
        tools.register(a_tool("candidate", schema_name="something_else"))


def test_a_duplicate_name_is_an_import_time_failure():
    """The reason there is one table: two `web_search` entries with two schemas is
    exactly the state this replaced, and it was invisible."""
    with pytest.raises(RegistryError, match="already registered"):
        tools.register(READ_TOOLS[0])


async def test_a_budget_refusal_is_not_an_error():
    """Refusals feed `run_tool_loop`'s stuck-loop breaker, so they must arrive as
    `ToolRefused` with something the model can act on, not as a crash."""
    context = ChatContext(session=None, max_searches=0, search_exhausted="No more searching.")
    with pytest.raises(ToolRefused, match="No more searching"):
        await tools.get("web_search").handler(context, {"query": "x"})


async def test_a_refusal_reaches_the_loop_as_a_refused_reply():
    """And the loop turns it into a reply rather than letting it escape: an
    exception out of `_dispatch` would end the turn instead of bouncing it."""
    harness = harness_offering("web_search", context=ChatContext(session=None, max_searches=0))
    reply = await _dispatch(harness, "web_search", {"query": "x"})
    assert reply.refused and "budget" in reply.content.lower()


async def test_an_unknown_tool_is_answered_not_raised():
    """Models invent tool names. That is a sentence back to the model, not a 500."""
    reply = await _dispatch(harness_offering("get_post"), "rm_rf", {})
    assert "Unknown tool" in reply.content


async def test_a_tool_the_harness_does_not_offer_is_unknown_to_it():
    """Being in the registry is not being on this run's table. A model that
    hallucinates another stage's tool is told it does not exist, which is the
    point of selecting by name."""
    assert "demote_story" in tools.TOOLS
    reply = await _dispatch(harness_offering("get_post"), "demote_story", {"reason": "x"})
    assert "Unknown tool" in reply.content


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


def test_every_tool_the_assistant_has_is_advertised_to_the_model():
    """Including the write tools: withholding them would leave the model unable to
    say what it wants to do, and it is the EXECUTION that is gated, not the ask."""
    harness = chat.chat_harness(None)
    advertised = {schema["function"]["name"] for schema in harness.schemas()}
    assert advertised == set(CHAT_TOOL_NAMES)


# --- the harness itself ------------------------------------------------------


def test_a_harness_naming_a_tool_that_does_not_exist_fails_at_build():
    """The whole reason tools are named rather than passed as objects: a typo is
    an error here, not a tool quietly missing from what a model is shown."""
    with pytest.raises(HarnessError, match="no tool named"):
        harness_offering("web_serach")


def test_a_harness_whose_context_cannot_serve_its_tools_fails_at_build():
    """`get_post` needs a session; a bare `ToolContext` does not have one. Better
    a startup failure than an AttributeError three tool calls into a run."""
    with pytest.raises(HarnessError, match="ToolContext"):
        harness_offering("get_post", context=tools.ToolContext())


def test_an_unfilled_prompt_slot_is_an_error():
    """A slot left as literal `{{demote}}` in what a model reads is the exact
    class of failure the harness exists to prevent, so it cannot be silent."""
    with pytest.raises(HarnessError, match="unfilled"):
        fill("a\n\n{{one}}\n\nb {{two}}", one="x")


def test_dropping_a_paragraph_leaves_no_hole():
    assert fill("a\n\n{{p}}\n\nb", p="") == "a\n\nb"
    assert fill("a\n\n{{p}}", p="") == "a"
    assert fill("a\n\n{{p}}\n\nb", p="mid") == "a\n\nmid\n\nb"


# --- the prompt's shape ------------------------------------------------------


def _row(role, content="x", **kw):
    return SimpleNamespace(
        id=kw.pop("id", 1),
        role=role,
        content=content,
        tool_name=kw.pop("tool_name", None),
        tool_call_id=kw.pop("tool_call_id", None),
        proposal_status=kw.pop("proposal_status", None),
        **kw,
    )


@pytest.mark.parametrize(
    "header,tail",
    [
        (None, None),
        ("The reader currently has this article open.", [{"role": "user", "content": "hi"}]),
        (
            None,
            [
                {"role": "assistant", "content": None, "tool_calls": []},
                {"role": "tool", "tool_call_id": "c1", "content": "done"},
            ],
        ),
    ],
)
def test_the_prompt_carries_exactly_one_system_message_and_it_is_first(header, tail):
    """Found live, 2026-08-12: the open-article header was appended as a SECOND
    system message, and Qwopus's chat template raises `System message must be at
    the beginning` on anything system-role that is not `loop.first`. llama-server
    reports that as a bare 400 naming no message, so the only cheap way to keep
    it fixed is to assert the shape here. The header is now a prompt slot, which
    is what makes a second system message unreachable rather than avoided."""
    rows = [
        _row("user"),
        _row("assistant"),
        _row("proposal", tool_name="write_article_from_url", proposal_status="approved"),
    ]
    harness = chat.chat_harness(None, open_article=header)
    messages = chat.build_messages(harness, rows, tail=tail)
    systems = [i for i, m in enumerate(messages) if m["role"] == "system"]
    assert systems == [0]
    if header:
        assert header in messages[0]["content"]
        assert "Do not paper over it." in messages[0]["content"]


def test_the_assistants_budgets_are_its_own_not_the_writers(monkeypatch):
    """One `web_search` handler serves both, so the numbers have to come off the
    context. Before one registry there were two handlers, each reading a
    different `settings` key, which is why this can be asserted at all."""
    monkeypatch.setattr(settings, "chat_max_searches", 2)
    monkeypatch.setattr(settings, "enrich_max_searches", 9)
    assert chat.chat_harness(None).context.max_searches == 2
