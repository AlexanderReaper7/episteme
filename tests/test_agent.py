"""Writer loop: it drives tools, holds editorial authority (write vs demote),
enforces budgets, and produces the schema-constrained draft in-conversation.

The research tools are patched on `episteme.research`, not on this module: one
`web_search` and one `fetch_page` serve every stage and live in `llm/tools.py`,
which imports them from there at call time (`llm/tools.py`'s docstring says why
there is only one of each).
"""

import json

import pytest

import episteme.llm.agent as agent
import episteme.research as research
from episteme.config import settings
from episteme.llm.harness import Harness
from episteme.llm.tools import Tool, ToolContext, WriteProposed, function

_DRAFT = {
    "title": "T",
    "summary": "S",
    "difficulty": "intermediate",
    "topics": ["physics"],
    # The quiz is mandatory (schemas._require_quiz), so a valid draft carries one.
    "sections": [
        {"type": "prose", "text": "Body"},
        {"type": "quiz", "questions": [
            {"question": "q?", "choices": ["a", "b"], "answer_index": 0,
             "explanation": "e"}]},
    ],
    "further_reading_urls": [],
}


def _assistant_toolcall(name, args, call_id="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": args}}]}


def _assistant_final(text):
    return {"role": "assistant", "content": text}


class ScriptedGateway:
    """Returns pre-scripted assistant messages for tool turns; draft turns
    (response_schema set) consume `draft_script` instead."""

    def __init__(self, script, draft_script=None):
        self.script = list(script)
        self.draft_script = list(draft_script or [_assistant_final(json.dumps(_DRAFT))])
        self.tool_turns = 0
        self.draft_turns = 0

    async def chat_messages(self, role, messages, tools=None, response_schema=None,
                            temperature=0.3):
        assert role == "main"  # the writer loop runs on the main model
        if response_schema is not None:
            self.draft_turns += 1
            return self.draft_script.pop(0)
        self.tool_turns += 1
        if not self.script:
            return _assistant_final("done")  # exhausted -> stop gathering
        return self.script.pop(0)


async def test_loop_gathers_then_writes(monkeypatch):
    # Two fetches (>= the stop threshold), an editorial note, then the draft turn.
    gw = ScriptedGateway([
        _assistant_toolcall("fetch_page", '{"url": "https://esawebb.org/images/potm2606a/"}', "c1"),
        _assistant_toolcall("fetch_page", '{"url": "https://ui.adsabs.harvard.edu/abs/paper"}', "c2"),
        _assistant_final("Fetched the ESA Webb page and the paper; those are the strongest sources."),
    ])
    monkeypatch.setattr(agent, "gateway", gw)

    async def fake_fetch(url):
        return {"url": url, "title": f"title of {url}",
                "text": f"A far richer description from {url}.", "links": []}

    monkeypatch.setattr(research, "fetch_page", fake_fetch)

    outcome = await agent.run_writer_loop("seed")

    assert outcome.decision == "write"
    assert outcome.draft is not None and outcome.draft.title == "T"
    assert [e["url"] for e in outcome.fetch_log] == [
        "https://esawebb.org/images/potm2606a/",
        "https://ui.adsabs.harvard.edu/abs/paper",
    ]
    assert "strongest sources" in outcome.notes
    assert outcome.gathered_chars > 0
    assert gw.draft_turns == 1


async def test_demote_story_skips_drafting(monkeypatch):
    gw = ScriptedGateway([
        _assistant_toolcall("fetch_page", '{"url": "https://a.org"}', "c1"),
        _assistant_toolcall("demote_story", '{"reason": "just a photo caption"}', "c2"),
    ])
    monkeypatch.setattr(agent, "gateway", gw)

    async def fake_fetch(url):
        return {"url": url, "title": url, "text": "thin", "links": []}

    monkeypatch.setattr(research, "fetch_page", fake_fetch)

    outcome = await agent.run_writer_loop("seed")

    assert outcome.decision == "aggregate"
    assert outcome.reason == "just a photo caption"
    assert outcome.draft is None
    assert gw.draft_turns == 0  # demotion must not request a draft


async def test_finish_research_ends_loop_and_carries_note(monkeypatch):
    """The story-291 failure inverted: the model signals 'done researching' with an
    explicit tool call (it used to reach for demote_story, killing the article).
    finish_research must end the loop, keep the note, and go to the draft."""
    gw = ScriptedGateway([
        _assistant_toolcall("fetch_page", '{"url": "https://a.org"}', "c1"),
        _assistant_toolcall(
            "finish_research", '{"note": "Nature piece is strongest."}', "c2"
        ),
        _assistant_toolcall("web_search", '{"query": "never reached"}', "c3"),
    ])
    monkeypatch.setattr(agent, "gateway", gw)

    async def fake_fetch(url):
        return {"url": url, "title": url, "text": "rich text", "links": []}

    monkeypatch.setattr(research, "fetch_page", fake_fetch)

    outcome = await agent.run_writer_loop("seed")

    assert outcome.decision == "write"
    assert outcome.notes == "Nature piece is strongest."
    assert gw.tool_turns == 2  # finish_research ended the loop; c3 never ran
    assert gw.draft_turns == 1


async def test_loop_nudges_when_model_narrates_instead_of_acting(monkeypatch):
    # Model returns a plan (no tool_calls) before fetching anything -> gets nudged,
    # then actually fetches on the next turn.
    monkeypatch.setattr(
        agent, "gateway",
        ScriptedGateway([
            _assistant_final("Next I will fetch the ESA Webb page and the paper."),
            _assistant_toolcall("fetch_page", '{"url": "https://esawebb.org/x"}', "c1"),
            _assistant_toolcall("fetch_page", '{"url": "https://ads/y"}', "c2"),
            _assistant_final("Done — fetched both."),
        ]),
    )

    async def fake_fetch(url):
        return {"url": url, "title": url, "text": f"rich text {url}", "links": []}

    monkeypatch.setattr(research, "fetch_page", fake_fetch)

    outcome = await agent.run_writer_loop("seed")
    assert len(outcome.fetch_log) == 2  # the nudge got it to act
    assert outcome.decision == "write"


async def test_fetch_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(settings, "enrich_max_fetches", 1)
    calls = []

    monkeypatch.setattr(
        agent, "gateway",
        ScriptedGateway([
            _assistant_toolcall("fetch_page", '{"url": "https://a.org"}', "c1"),
            _assistant_toolcall("fetch_page", '{"url": "https://b.org"}', "c2"),
            _assistant_final("done"),
        ]),
    )

    async def fake_fetch(url):
        calls.append(url)
        return {"url": url, "title": url, "text": "text", "links": []}

    monkeypatch.setattr(research, "fetch_page", fake_fetch)

    outcome = await agent.run_writer_loop("seed")

    assert calls == ["https://a.org"]  # second fetch refused by the budget
    assert len(outcome.fetch_log) == 1


async def test_stuck_budget_loop_forces_draft(monkeypatch):
    """The story-371 failure: after exhausting its search budget, the model kept
    re-issuing the same searches every turn. Two consecutive all-refused turns must
    break to the draft instead of burning the remaining step budget."""
    monkeypatch.setattr(settings, "enrich_max_searches", 1)
    monkeypatch.setattr(settings, "enrich_max_steps", 12)

    class AlwaysSearch:
        tool_turns = 0

        async def chat_messages(self, role, messages, tools=None, response_schema=None,
                                temperature=0.3):
            if response_schema is not None:
                return _assistant_final(json.dumps(_DRAFT))
            self.tool_turns += 1
            return _assistant_toolcall("web_search", '{"query": "x"}', f"c{self.tool_turns}")

    gw = AlwaysSearch()
    monkeypatch.setattr(agent, "gateway", gw)

    async def fake_search(q):
        return [{"title": "t", "url": "https://x.org", "snippet": "s"}]

    monkeypatch.setattr(research, "web_search", fake_search)

    outcome = await agent.run_writer_loop("seed")
    # Turn 1 spends the budget; turns 2 and 3 are all-refused -> break. Never 12.
    assert gw.tool_turns == 3
    assert outcome.decision == "write"


async def test_draft_validation_retries_with_repair(monkeypatch):
    gw = ScriptedGateway(
        [_assistant_final("note")],
        draft_script=[
            _assistant_final('{"title": "T"}'),  # invalid: missing fields
            _assistant_final(json.dumps(_DRAFT)),
        ],
    )
    monkeypatch.setattr(agent, "gateway", gw)
    outcome = await agent.run_writer_loop("seed")
    assert outcome.decision == "write"
    assert outcome.draft is not None
    assert gw.draft_turns == 2


async def test_draft_failure_demotes(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_json_retries", 1)
    gw = ScriptedGateway(
        [_assistant_final("note")],
        draft_script=[_assistant_final("not json"), _assistant_final("still not json")],
    )
    monkeypatch.setattr(agent, "gateway", gw)
    outcome = await agent.run_writer_loop("seed")
    assert outcome.decision == "aggregate"
    assert "validation" in outcome.reason


async def test_loop_stops_at_max_steps(monkeypatch):
    monkeypatch.setattr(settings, "enrich_max_steps", 3)
    monkeypatch.setattr(settings, "enrich_max_searches", 99)

    # Always asks to search — never volunteers a final message.
    class AlwaysSearch:
        tool_turns = 0

        async def chat_messages(self, role, messages, tools=None, response_schema=None,
                                temperature=0.3):
            if response_schema is not None:
                return _assistant_final(json.dumps(_DRAFT))
            self.tool_turns += 1
            return _assistant_toolcall("web_search", '{"query": "x"}', f"c{self.tool_turns}")

    gw = AlwaysSearch()
    monkeypatch.setattr(agent, "gateway", gw)

    async def fake_search(q):
        return [{"title": "t", "url": "https://x.org", "snippet": "s"}]

    monkeypatch.setattr(research, "web_search", fake_search)

    outcome = await agent.run_writer_loop("seed")
    assert gw.tool_turns == 3  # capped at enrich_max_steps, no runaway
    assert outcome.decision == "write"  # still drafts from what it has


# --- the `turn` seam ---------------------------------------------------------
# `run_tool_loop` is shared by the writer, QA and the streaming chat agent, which
# differ in exactly one line: how a turn is taken. The tests above already prove
# the writer still works; these pin the seam itself, so a change to the loop that
# quietly special-cases the injected path is a failure rather than a surprise in
# the browser.

_SCRIPT = [
    _assistant_toolcall("look", '{"q": "a"}', "c1"),
    _assistant_toolcall("look", '{"q": "b"}', "c2"),
    _assistant_final("that is everything"),
]


def _harness(*tools_: Tool, context=None, max_steps=6) -> Harness:
    """A harness over tools that exist only for this test.

    Built directly rather than through `build`, because registering them would
    put them in the table every other stage selects from - and the registry
    refusing a duplicate name is a property another test relies on.
    """
    return Harness(
        name="test",
        system="sys",
        role="main",
        max_steps=max_steps,
        wall_clock_seconds=30,
        context=context if context is not None else ToolContext(),
        _tools=list(tools_),
    )


async def _run_once(turn, seen: list):
    """One identical run of the loop, driven either by a `turn` or by the gateway."""

    async def look(ctx, args):
        seen.append(("look", args))
        return f"result for {args.get('q')}"

    harness = _harness(Tool(name="look", schema=function("look", "d", {}, []), handler=look))
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    closing = await agent.run_tool_loop(harness, messages, turn=turn)
    return closing, messages, seen


async def test_an_injected_turn_changes_nothing_but_where_the_message_came_from(monkeypatch):
    """The seam's whole claim. Asserted by running one script both ways and
    comparing the transcripts rather than by hand-written assertions per branch:
    a divergence anywhere in dispatch, the budgets or the exits then shows up as a
    diff, and the equivalence stays checked as the loop grows."""
    gw = ScriptedGateway(list(_SCRIPT))
    monkeypatch.setattr(agent, "gateway", gw)
    through_gateway = await _run_once(None, [])

    scripted = list(_SCRIPT)
    saw: list[int] = []

    async def turn(messages, tools):
        # The loop passes the live transcript and the same tool list every time;
        # the streaming agent relies on both to build its request.
        saw.append(len(messages))
        assert [t["function"]["name"] for t in tools] == ["look"]
        return scripted.pop(0)

    exploded = ScriptedGateway([])

    async def refuse(*a, **kw):
        raise AssertionError("the gateway must not be called when `turn` is supplied")

    exploded.chat_messages = refuse
    monkeypatch.setattr(agent, "gateway", exploded)
    through_turn = await _run_once(turn, [])

    assert through_turn == through_gateway
    assert saw == [2, 4, 6]  # two seeds, then +1 assistant +1 tool result per turn


async def test_a_terminal_tool_ends_an_injected_run_before_the_next_turn():
    """Which tools end a run is read off the registry, not decided by a dispatcher
    that each stage writes for itself. Nothing may ask the model for another turn
    after one fires."""
    turns = 0

    async def turn(messages, tools):
        nonlocal turns
        turns += 1
        return _assistant_toolcall("wrap_up", "{}", f"c{turns}")

    async def wrap_up(ctx, args):
        return "closed"

    harness = _harness(
        Tool(name="wrap_up", schema=function("wrap_up", "d", {}, []),
             handler=wrap_up, terminal=True)
    )
    messages: list[dict] = []
    closing = await agent.run_tool_loop(harness, messages, turn=turn)
    assert turns == 1
    assert closing == ""
    assert messages[-1]["content"] == "closed"


async def test_a_write_tool_leaves_the_loop_instead_of_running():
    """The assistant's permission gate, seen from the loop's side: `WriteProposed`
    is the one exception that is not turned into a tool result, because the reader
    has to answer before anything else in the run is worth doing (0036). It fires
    for ANY harness, not only the assistant's."""

    async def turn(messages, tools):
        return _assistant_toolcall("do_it", '{"url": "u"}', "c1")

    async def never(ctx, args):
        raise AssertionError("a write tool executed inside the loop")

    harness = _harness(
        Tool(name="do_it", schema=function("do_it", "d", {}, []), handler=never,
             writes=True, describe=lambda args: f"do {args['url']}")
    )
    with pytest.raises(WriteProposed) as raised:
        await agent.run_tool_loop(harness, [], turn=turn)
    assert raised.value.tool.name == "do_it"
    assert raised.value.tool_args == {"url": "u"}


async def test_on_tool_is_told_before_the_tool_runs():
    """What the assistant's rail shows while a main-model call is in flight. The
    order is the point: announced first, run second, or the notice arrives after
    the wait it exists to explain."""
    events: list[str] = []

    async def turn(messages, tools):
        return _assistant_toolcall("look", '{"q": "a"}', "c1") if not events else (
            _assistant_final("done")
        )

    async def look(ctx, args):
        events.append("ran")
        return "ok"

    async def on_tool(name, args):
        events.append(f"announced {name} {args['q']}")

    harness = _harness(Tool(name="look", schema=function("look", "d", {}, []), handler=look))
    harness.on_tool = on_tool
    await agent.run_tool_loop(harness, [], turn=turn)
    assert events == ["announced look a", "ran"]
