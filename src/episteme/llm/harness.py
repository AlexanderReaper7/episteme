"""A harness: everything one agentic run needs, decided in one place.

Three stages drive `agent.run_tool_loop` - the writer, the QA reviewer, the
reader's assistant - and each used to assemble its own system prompt, tool list,
model role and budgets by hand, at its own call site. Nothing checked they
agreed, and they did not: `pipeline.py` withheld `demote_story` from a
reader-requested story's TOOL LIST while `WRITER_AGENT_SYSTEM` went on telling
the model to call it, and `agent._SEARCH_EXHAUSTED` named it again when a budget
bounced. Only half of 0037's rule was ever built, and post 543's provenance shows
the paragraph on a story whose tool list did not carry the tool.

A harness is the fix and the unit: **which model, what it is told, what it may
do, how long it gets, and how the run ends**. One object, built per run, so the
prompt and the tool list cannot be chosen separately because they are not chosen
separately any more.

**A condition known at build time is interpolated into the prompt.** The system
prompt is a template with named slots; a harness fills them from the same flags
that select the tools. `writer(allow_demote=False)` drops `demote_story` from the
list and fills the demote slot with nothing, in one expression. There is no
second place to forget.

**A condition that arrives mid-run is a message.** A tool list can grow after the
conversation has started - the QA reviewer gains `rerender` the moment an edit
introduces a section only a render can vouch for - and the model cannot be
expected to notice a tool appearing. `Harness.offer` adds the tool and returns
the sentence that says so, which the caller puts in the conversation. Growing the
table silently is what is not wanted; the model reading about a tool it does not
have is the other half of the same mistake.

**Budgets are read at build time**, not baked into a module constant, so
`settings.qa_max_steps` stays live-configurable and a harness is a value rather
than a global.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .gateway import Role
from .tools import RegistryError, Tool, ToolContext, get

log = logging.getLogger("episteme.harness")


class HarnessError(Exception):
    """A harness that must not be run."""


@dataclass
class Closing:
    """The unconditional turn a run ends with, after the tool loop stops.

    Both the writer and the QA reviewer end by asking for one grammar-constrained,
    pydantic-validated reply in the SAME conversation, so the provenance view
    renders tools, judgment and output as one exchange. The assistant has no
    closing turn: its answer IS the loop's free text.
    """

    #: The user message that asks for it.
    request: str
    #: The pydantic model the reply is validated against.
    schema: type[BaseModel]
    temperature: float = 0.3


@dataclass
class Harness:
    """One agentic run's whole configuration. Built per run, never a constant."""

    #: For logs and tests. `writer`, `writer.requested`, `qa`, `qa.visual`, `chat`.
    name: str
    #: The finished system prompt, every slot already filled.
    system: str
    #: The gateway role, so a harness names its model the way everything else
    #: does: by what it is for, never by a model name or a URL (0003).
    role: Role
    max_steps: int
    wall_clock_seconds: float
    #: Per-run state handed to every handler. Its type is checked against every
    #: tool's declared `context`.
    context: ToolContext
    _tools: list[Tool] = field(default_factory=list)
    #: Consulted when the model answers with no tool calls: a string nudges it and
    #: continues, None accepts the answer and stops.
    on_idle: Callable[[str], str | None] | None = None
    #: Awaited just before each tool call, name and arguments, when somebody is
    #: watching the run. The assistant's rail uses it to say what it is doing
    #: while a main-model call is in flight; the writer and QA leave it unset.
    #: Fires before the tool is even looked up, so an unknown or a write-gated
    #: name is announced too - the reader saw the model reach for it either way.
    on_tool: Callable[[str | None, dict], Awaitable[None]] | None = None
    closing: Closing | None = None
    #: Turns where EVERY call bounced off an exhausted budget before the loop
    #: gives up on a model that is not going to stop asking.
    max_refused_turns: int = 2

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools)

    @property
    def names(self) -> set[str]:
        return {tool.name for tool in self._tools}

    def schemas(self) -> list[dict]:
        """What the model is shown. Read fresh each turn, so `offer` takes effect."""
        return [tool.schema for tool in self._tools]

    def tool(self, name: str | None) -> Tool | None:
        """The tool by that name IF this harness offers it.

        A name the harness does not offer returns None even when it is in the
        registry, which is what stops a model hallucinating another stage's tool
        into existence.
        """
        return next((t for t in self._tools if t.name == name), None)

    def offer(self, name: str, because: str) -> str:
        """Add a tool mid-run and return the sentence that announces it.

        The return value is not optional decoration. A tool list that grows
        without the model being told is a table it has no reason to re-read, and
        the caller is expected to put this sentence into the conversation - as a
        tool reply, or as a message of its own. Returns "" if the tool is already
        offered, so a caller may call this on every edit.

        Growing the table re-prefills the prompt, since tools are rendered ahead
        of the messages. That is the deliberate trade wherever this is used.

        A terminal tool stays last. It is the run's closing affordance and worth
        being the last thing the model reads, so a tool offered mid-run slots in
        ahead of it rather than after it.
        """
        if self.tool(name) is not None:
            return ""
        added = get(name)
        _check_context(self.name, added, self.context)
        at = next(
            (i for i, tool in enumerate(self._tools) if tool.terminal), len(self._tools)
        )
        self._tools.insert(at, added)
        log.info("%s: offered %s mid-run", self.name, name)
        return because


def _check_context(harness_name: str, tool: Tool, context: ToolContext) -> None:
    if not isinstance(context, tool.context):
        raise HarnessError(
            f"{harness_name} offers {tool.name}, which needs a "
            f"{tool.context.__name__}, but the harness context is a "
            f"{type(context).__name__}"
        )


def build(
    *,
    name: str,
    system: str,
    tools: list[str],
    role: Role,
    max_steps: int,
    wall_clock_seconds: float,
    context: ToolContext,
    on_idle: Callable[[str], str | None] | None = None,
    on_tool: Callable[[str | None, dict], Awaitable[None]] | None = None,
    closing: Closing | None = None,
    max_refused_turns: int = 2,
) -> Harness:
    """Assemble a harness, refusing one that cannot run.

    Tools are named, not passed as objects, so a typo is an error here rather
    than a tool silently missing from the list a model is shown.
    """
    try:
        selected = [get(tool_name) for tool_name in tools]
    except RegistryError as exc:
        raise HarnessError(f"{name}: {exc}") from exc
    if len(set(tools)) != len(tools):
        raise HarnessError(f"{name}: the same tool is named twice")
    harness = Harness(
        name=name,
        system=system,
        role=role,
        max_steps=max_steps,
        wall_clock_seconds=wall_clock_seconds,
        context=context,
        _tools=selected,
        on_idle=on_idle,
        on_tool=on_tool,
        closing=closing,
        max_refused_turns=max_refused_turns,
    )
    for tool in selected:
        _check_context(name, tool, context)
    # The back-reference, so a handler can change the run it is part of. Set here
    # rather than by the caller, because a context handed to `build` and then not
    # wired up would fail only on the rare path that uses it.
    context.harness = harness
    return harness


def fill(template: str, **slots: Any) -> str:
    """A prompt template with its conditional paragraphs filled in.

    Slots are named `{{demote}}` rather than `{demote}` so a prompt may contain
    JSON braces, which several of them do. A slot the caller does not fill is an
    error rather than a paragraph silently left as literal text in what a model
    reads - the failure this whole module exists to make impossible.

    A slot filled with "" leaves no hole in the prose: whatever blank lines it
    was separated by collapse back to one paragraph break, and a slot at the end
    of the template leaves no trailing blank line. That is done once over the
    finished text rather than per slot, so it holds wherever the slot sits.
    """
    out = template
    for key, value in slots.items():
        marker = "{{" + key + "}}"
        if marker not in out:
            raise HarnessError(f"prompt has no slot {marker}")
        out = out.replace(marker, str(value).strip())
    left = [chunk.split("}}")[0] for chunk in out.split("{{")[1:]]
    if left:
        raise HarnessError(f"prompt slots left unfilled: {', '.join(sorted(set(left)))}")
    return re.sub(r"\n{3,}", "\n\n", out).strip()
