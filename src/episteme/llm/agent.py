"""The agentic tool loop, and the writer built on it.

`run_tool_loop` is the generic driver all three agentic stages share: it turns the
model, dispatches whatever tools it calls, enforces step and wall-clock budgets,
and stops on a terminal tool. What each stage differs on - the prompt, the tools,
the model, the budgets, how the run ends - is a `harness.Harness`, built per run,
so those cannot be chosen separately any more. They used to be, and they drifted:
see `harness.py`'s docstring for the bug that motivated it.

The writer's run (spec §7): the main model may `web_search` and `fetch_page` to
research as it sees fit, may `demote_story` when the material does not merit an
article **and the harness offers that tool**, and finally produces the post draft
in a grammar-constrained, pydantic-validated turn in the SAME conversation
(`request_validated`), so the provenance view renders tools, judgment and output
as one exchange.

The blast radius of a prompt injection in fetched content stays small. The only
consequential writer tool is `demote_story`, which at worst sends one story to
the aggregation stream instead of the article feed - and no `writes=True` tool
executes from this loop at all, in any harness: it raises `WriteProposed` and
waits for the reader (0036).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..config import settings
from . import gateway
from .gateway import LLMUnavailable, Role
from .harness import Closing, Harness, build, fill
from .observe import llm_conversation
from .prompts import WRITER_AGENT_SYSTEM, WRITER_MAY_DEMOTE, WRITER_MUST_WRITE
from .schemas import PostDraft
from .tools import (
    Tool,
    ToolContext,
    ToolRefused,
    WriteProposed,
    function,
    parse_args,
    register,
)

log = logging.getLogger("episteme.agent")

T = TypeVar("T", bound=BaseModel)


@dataclass
class WriterContext(ToolContext):
    """The writer's per-run state, on top of the shared research budgets."""

    nudges: int = 0
    notes: str = ""
    demoted: str | None = None  # demote_story's reason; checked after the loop


async def _finish_research(ctx: WriterContext, args: dict) -> str:
    # The explicit "done researching" affordance. Without it, tool-tuned models
    # reach for the only other terminal tool - story 291 called demote_story with
    # reason "no need to demote" just to end research.
    ctx.notes = str(args.get("note", "")) or ctx.notes
    return "Research phase closed."


async def _demote_story(ctx: WriterContext, args: dict) -> str:
    ctx.demoted = str(args.get("reason", "")) or "writer demoted"
    return "Story demoted to the aggregation stream."


FINISH_RESEARCH = register(
    Tool(
        name="finish_research",
        schema=function(
            "finish_research",
            "Declare research COMPLETE and move on to writing the post. Call this "
            "once you have gathered enough to write with depth and accuracy.",
            {
                "note": {
                    "type": "string",
                    "description": "Short editorial note: what you found and which "
                    "sources are strongest",
                }
            },
            ["note"],
        ),
        handler=_finish_research,
        context=WriterContext,
        terminal=True,
    )
)

DEMOTE_STORY = register(
    Tool(
        name="demote_story",
        schema=function(
            "demote_story",
            "DECLINE to write an article for this story because even after research "
            "the material is too thin or of too little learning value. The story "
            "falls back to the aggregation stream, a fine outcome for minor items. Do "
            "NOT call this when the material IS worth an article - to proceed to "
            "writing, call finish_research instead.",
            {"reason": {"type": "string", "description": "One sentence"}},
            ["reason"],
        ),
        handler=_demote_story,
        context=WriterContext,
        terminal=True,
    )
)

# Tool-use models sometimes *describe* the next fetch instead of calling it.
# When one stops early without having fetched much, nudge it to actually act.
_NUDGE = (
    "Do not describe what you will do next - actually call the tools now. Fetch the "
    "key sources you identified (the primary/expansive pages), then stop."
)
_MAX_NUDGES = 2
_MIN_FETCHES_BEFORE_STOP = 2

_DRAFT_REQUEST = (
    "Now write the post. Respond with ONLY the JSON object for the post draft - "
    "structure, length, and emphasis are your editorial call; the schema constrains "
    "only the shape."
)

#: A model that keeps re-issuing tool calls after its budgets ran dry (story 371
#: repeated the same two searches for six turns) gets this many all-refused turns
#: before the loop stops burning main-model time and forces the draft.
_MAX_REFUSED_TURNS = 2


@dataclass
class WriteOutcome:
    decision: str  # "write" | "aggregate"
    reason: str = ""
    draft: PostDraft | None = None  # set when decision == "write"
    fetch_log: list[dict] = field(default_factory=list)  # [{url,title}], final post-redirect URLs
    notes: str = ""  # the model's editorial note before drafting (may be "")
    gathered_chars: int = 0  # fetched text volume, for the deterministic thin-gate


@dataclass
class ToolReply:
    """What a handler returns when a plain string is not enough.

    A handler may return a `str`, which becomes the tool result verbatim. It
    returns one of these when it needs to say something about the LOOP as well:
    that it bounced off a budget, or that it produced something a tool result
    cannot carry.
    """

    content: str  # the `role: "tool"` message body the model sees
    refused: bool = False  # bounced off a budget; feeds the stuck-loop breaker
    # An extra message appended AFTER this turn's tool results. A tool result must be
    # a plain `role: "tool"` string, so a tool that produces something else (qa's
    # `rerender`, which produces a screenshot) hands the image back as its own
    # follow-up user message instead of smuggling it into the tool payload.
    follow_up: dict | None = None


# How one turn is taken. The loop only needs "messages plus tools in, one complete
# assistant message out"; whether that arrived in a single response or was rebuilt
# from a token stream is not the loop's business.
Turn = Callable[[list[dict], list[dict]], Awaitable[dict]]


async def run_tool_loop(
    harness: Harness,
    messages: list[dict],
    *,
    turn: Turn | None = None,
) -> str:
    """Drive `messages` through `harness`'s bounded tool loop, in place. Returns the
    model's closing free text (empty when it stopped on a terminal tool).

    Three independent ways out, all bounded: a tool the registry marks `terminal`,
    `harness.max_steps` turns, or `harness.wall_clock_seconds`. `harness.on_idle`
    is consulted when the model answers with no tool calls at all - a string
    nudges it and continues, None accepts the answer and stops. The caller is
    expected to do something unconditional afterwards (`harness.closing`), so a
    run that exhausts its budget still produces output.

    `turn` overrides how a turn is taken, and is the seam the streaming chat agent
    enters through: it hands back the same assembled message a blocking call would,
    having pushed the tokens somewhere along the way. Budgets, dispatch, the
    stuck-loop breaker and the terminal-tool exit are then shared rather than
    reimplemented per stage. Omitted, it is `gateway.chat_messages(role, ...)`.

    **A `writes=True` tool never executes here.** It raises `WriteProposed`, which
    is the only behaviour, for every harness, so the reader's approval card is not
    a convention the assistant follows but the only path that exists (0036).
    """
    deadline = time.monotonic() + harness.wall_clock_seconds
    refused_turns = 0
    for _ in range(harness.max_steps):
        if time.monotonic() > deadline:
            log.info("%s: hit wall-clock budget", harness.name)
            break
        # Read the tool list fresh each turn, so `harness.offer` takes effect.
        schemas = harness.schemas()
        if turn is not None:
            message = await turn(messages, schemas)
        else:
            message = await gateway.chat_messages(harness.role, messages, tools=schemas)
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            nudge = harness.on_idle(message.get("content") or "") if harness.on_idle else None
            if nudge is None:
                return message.get("content") or ""
            messages.append({"role": "user", "content": nudge})
            continue
        turn_all_refused = True
        stop = False
        follow_ups: list[dict] = []
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name")
            reply = await _dispatch(harness, name, parse_args(fn.get("arguments")))
            if not reply.refused:
                turn_all_refused = False
            messages.append(
                {"role": "tool", "tool_call_id": call.get("id", ""), "content": reply.content}
            )
            if reply.follow_up is not None:
                follow_ups.append(reply.follow_up)
            tool = harness.tool(name)
            stop = stop or (tool is not None and tool.terminal)
        messages.extend(follow_ups)
        if stop:
            break
        # Stuck-loop breaker: turns where EVERY tool call bounced off an exhausted
        # budget gather nothing - after a couple of those, stop burning model time.
        refused_turns = refused_turns + 1 if turn_all_refused else 0
        if refused_turns >= harness.max_refused_turns:
            log.info("%s: stuck on exhausted budgets; stopping", harness.name)
            break
    return ""


async def _dispatch(harness: Harness, name: str | None, args: dict) -> ToolReply:
    """Run one tool call under `harness`, and turn whatever comes back into a reply.

    A name the harness does not offer is answered rather than raised: models do
    hallucinate a tool from another stage, and telling one it does not exist is
    cheaper than ending the run.

    `WriteProposed` is the one exception that leaves the loop entirely, because
    nothing after it in this run is worth doing: the reader has to answer before
    the effect can happen, and a turn cannot be held open across that (0036).
    """
    if harness.on_tool is not None:
        await harness.on_tool(name, args)
    tool = harness.tool(name)
    if tool is None:
        return ToolReply(f"Unknown tool: {name}")
    if tool.writes:
        # The gate. Not a check this function remembers to do for the assistant -
        # it is here, above every handler, for every harness.
        raise WriteProposed(tool, args)
    try:
        result = await tool.handler(harness.context, args)
    except ToolRefused as exc:
        return ToolReply(str(exc), refused=True)
    except WriteProposed:
        raise
    except LLMUnavailable:
        # A tool that reaches a model has the same problem the loop does. Telling
        # the model to route around a dead endpoint asks it to spend a turn it
        # cannot take, on a server that will not answer the turn either.
        raise
    except Exception as exc:  # a tool's own failure is the model's problem to route around
        log.warning("%s: %s failed: %s", harness.name, name, exc)
        return ToolReply(f"Tool error: {exc}")
    return result if isinstance(result, ToolReply) else ToolReply(str(result))


async def request_validated(
    role: Role, messages: list[dict], schema: type[T], temperature: float = 0.3
) -> T | None:
    """Ask for a grammar-constrained, pydantic-validated reply appended to the SAME
    conversation, with repair-prompt retries kept in the transcript. Returns None
    after exhausting retries (callers decide the fallback). Shared by the writer's
    final draft turn and the QA stage's review turns."""
    json_schema = schema.model_json_schema()
    last_error: ValidationError | None = None
    for attempt in range(1 + settings.llm_max_json_retries):
        message = await gateway.chat_messages(
            role, messages, response_schema=json_schema, temperature=temperature
        )
        messages.append(message)
        try:
            return schema.model_validate_json(message.get("content") or "")
        except ValidationError as exc:
            last_error = exc
            log.warning("Invalid %s (attempt %d): %s", schema.__name__, attempt + 1, exc)
            messages.append(
                {
                    "role": "user",
                    "content": f"Your previous response failed validation with:\n{exc}\n"
                    "Respond again with ONLY valid JSON matching the schema.",
                }
            )
    log.warning("%s failed validation after retries: %s", schema.__name__, last_error)
    return None


async def close(harness: Harness, messages: list[dict], schema: type[T]) -> T | None:
    """Run `harness.closing` against the same conversation. Returns None if there
    is no closing turn, or if it failed validation after retries."""
    if harness.closing is None:
        return None
    messages.append({"role": "user", "content": harness.closing.request})
    return await request_validated(
        harness.role, messages, schema, temperature=harness.closing.temperature
    )


# --- The writer ---------------------------------------------------------------


def writer_harness(*, allow_demote: bool = True) -> Harness:
    """The writer's harness. `allow_demote=False` is a reader-requested story.

    One flag decides three things that must agree and previously did not: whether
    `demote_story` is on the table, whether the prompt tells the model to reach
    for it, and what a bounced search budget offers as a way out. Withholding the
    tool is still the enforcement - a story the reader asked for cannot be
    declined, because declining is a tool call and the tool is absent - but the
    model is no longer told to make a call it cannot make.
    """
    escape = " or call demote_story if it isn't enough for an article" if allow_demote else ""
    context = WriterContext(
        max_searches=settings.enrich_max_searches,
        max_fetches=settings.enrich_max_fetches,
        search_exhausted=(
            f"Search budget exhausted; stop searching. Write from what you have{escape}."
        ),
        fetch_exhausted=(
            f"Fetch budget exhausted; stop fetching. Write from what you have{escape}."
        ),
    )

    def on_idle(_text: str) -> str | None:
        # The model may have narrated its next fetch instead of calling it - nudge
        # if it stopped early AND there is still fetch budget to act on.
        if (
            context.nudges < _MAX_NUDGES
            and len(context.fetch_log) < _MIN_FETCHES_BEFORE_STOP
            and context.fetches < settings.enrich_max_fetches
        ):
            context.nudges += 1
            return _NUDGE
        return None

    tools = ["web_search", "fetch_page", "finish_research"]
    if allow_demote:
        tools.append("demote_story")
    return build(
        name="writer" if allow_demote else "writer.requested",
        system=fill(
            WRITER_AGENT_SYSTEM,
            demote=WRITER_MAY_DEMOTE if allow_demote else WRITER_MUST_WRITE,
        ),
        tools=tools,
        role="main",
        # enrich_enabled=False is the research kill-switch (e.g. SearXNG down):
        # no tool loop at all, and the draft comes straight from the source items.
        max_steps=settings.enrich_max_steps if settings.enrich_enabled else 0,
        wall_clock_seconds=settings.enrich_wall_clock_seconds,
        context=context,
        on_idle=on_idle,
        closing=Closing(request=_DRAFT_REQUEST, schema=PostDraft, temperature=0.4),
        max_refused_turns=_MAX_REFUSED_TURNS,
    )


async def run_writer_loop(seed: str, *, allow_demote: bool = True) -> WriteOutcome:
    with llm_conversation():  # calls log as one chain (delta storage, see observe)
        return await _run_writer_loop(seed, allow_demote=allow_demote)


async def _run_writer_loop(seed: str, *, allow_demote: bool) -> WriteOutcome:
    harness = writer_harness(allow_demote=allow_demote)
    context: WriterContext = harness.context  # type: ignore[assignment]
    messages: list[dict] = [
        {"role": "system", "content": harness.system},
        {"role": "user", "content": seed},
    ]
    notes = await run_tool_loop(harness, messages)
    context.notes = notes or context.notes

    if context.demoted is not None:
        return WriteOutcome(
            decision="aggregate",
            reason=context.demoted,
            fetch_log=context.fetch_log,
            gathered_chars=context.gathered_chars,
        )

    draft = await close(harness, messages, PostDraft)
    if draft is None:
        return WriteOutcome(
            decision="aggregate",
            reason="draft failed schema validation after retries",
            fetch_log=context.fetch_log,
            notes=context.notes,
            gathered_chars=context.gathered_chars,
        )
    return WriteOutcome(
        decision="write",
        draft=draft,
        fetch_log=context.fetch_log,
        notes=context.notes,
        gathered_chars=context.gathered_chars,
    )
