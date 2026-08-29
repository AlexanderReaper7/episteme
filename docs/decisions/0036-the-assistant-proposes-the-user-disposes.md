# 0036. The assistant proposes, the reader disposes: a write tool never executes

- Date: 2026-08-11
- Status: accepted
- Rule: a `writes=True` tool is unreachable from `dispatch`. It raises `WriteProposed`, and only an approved row reaches its handler.

## Context

Everything the assistant can reach, it can be *talked into* reaching. `fetch_page` and `web_search` put arbitrary third-party text into the conversation, and there is no auth anywhere in this app: single-user forever was decided long ago, so there is no second factor, no role check, and nothing between "the model decided to do X" and X happening. The user's requirement was stated in the same sentence as the feature: *"it must prompt the user for permission before doing anything that can write"*.

The obvious implementation is a check inside each write handler. That is the version that fails, because it is a convention: it holds for as long as every future tool remembers to open with the same four lines, and the failure mode of forgetting is silent, immediate and irreversible.

## Decision

**The gate is structural, not conventional.** `chat_tools.dispatch` has one branch for a write tool and it is not a call:

```python
async def dispatch(ctx: ChatContext, name: str | None, args: dict) -> str:
    tool = TOOLS.get(name or "")
    if tool is None:
        return f"Unknown tool: {name}"
    if tool.writes:
        raise WriteProposed(tool, args)
    return await tool.handler(ctx, args)
```

`execute_approved` is a separate function, and it is the only other place a handler is called. So "did the reader approve this" is not a question any handler asks; it is the difference between two call sites, one of which the model cannot reach. A new write tool is gated by having been added to the registry with `writes=True`, which is also the thing that makes it exist.

**An exception, not a return value.** A proposal is not a result, and nothing that handles results should be able to mistake it for one. `WriteProposed` carries the tool and the validated arguments; the loop catches it, parks with `ToolReply(stop=True)`, and the turn ends there.

> `WriteProposed.args` would silently shadow `BaseException.args`, which is the message tuple. The attribute is `tool_args`. A test caught this; a rendered approval card reading "Write an article from ('url',)" is what it caught it instead of.

**Approval executes server-side.** The card posts to `POST /chat/proposal/{id}/resolve?approve=true`, which flips the row, runs `execute_approved`, splices a synthetic assistant tool-call plus its result into a rebuilt history, and resumes the loop. The browser authorizes; it never carries the effect. So the worst a prompt injection in a fetched page achieves is *a card appearing that asks you a question*.

**A proposal is a row, not stream state.** `chat_messages.proposal_status` is `pending|approved|rejected`, so a proposal survives a reload, a navigation and a browser restart, and the scrollback renders a settled one as what it was rather than as a live prompt.

**The property is tested mechanically, over the registry.** `tests/test_chat_tools.py` parametrizes over `WRITE_TOOLS` and replaces each handler with a tripwire that raises, which is what makes "did not execute" observable rather than assumed. A second write tool inherits the test by existing. `validate_registry` runs at import and rejects a `writes=True` tool with no `describe`, because a card the reader cannot read is not consent.

## Rejected

- **A check inside each handler.** One policy stated twelve times is twelve audits, and the twelfth is the one that ships wrong.
- **A confirm flag in the arguments** (`{"url": ..., "confirmed": true}`). The model fills the arguments. It would be asking itself.
- **A blanket "ask before any tool".** Reads are how the assistant answers anything; a prompt per `get_post` trains the reader to click through, which is worse than no prompt.
- **Approving in the browser and POSTing the effect.** Moves the decision to the client, where a stray fetch is the same shape as an approval.

## Consequences

- Phase 3's administrative tools (`add_source`, `add_voice`, `run_job`, `pause_pipeline`) need nothing new to be safe. They need `writes=True` and a `describe`.
- The loop stops mid-conversation on a proposal. That is visible, and it is meant to be: nothing more should happen until the reader answers.
- `describe(args)` is the security surface that remains. It must state the real effect, not the tool's name, because it is the only thing the reader reads.
- **Amended by 0049 (2026-08-29).** The gate moved from `chat_tools.dispatch` to `agent._dispatch`, which is the loop every stage shares, so it now covers a `writes=True` tool in ANY harness rather than only the assistant's. `WriteProposed` leaves the loop entirely instead of parking it with a stop flag. `validate_registry` became `tools._validate`, run by `register` at import, over one table. `tests/test_chat_tools.py` iterates that whole table, so the tripwire property extends to write tools the writer or the reviewer might grow.
