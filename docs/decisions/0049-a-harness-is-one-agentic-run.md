# 0049. A harness is one agentic run, and the prompt cannot disagree with the tool list

- Date: 2026-08-29
- Status: accepted
- Rule: a stage never assembles a system prompt, a tool list, a model role and a set of budgets separately. It builds a **harness** (`llm/harness.py`), which holds all of them, and hands that to `agent.run_tool_loop`. A condition known before the run starts fills a **slot in the prompt**; a condition that arrives mid-run is a **message**. Tools come from one registry (`llm/tools.py`) and are selected **by name**, so a harness naming a tool that does not exist fails at build.

## What went wrong

0037 says a reader-requested story cannot be declined: the writer must not reach for `demote_story`, because the reader asked for this article specifically. Half of that was built. `pipeline.py` withheld `demote_story` from the **tool list**, and nothing touched the prompt:

```
WRITER_AGENT_SYSTEM  ->  "...call demote_story with a one-sentence reason."
agent._SEARCH_EXHAUSTED  ->  "...or call demote_story if it isn't enough for an article."
```

Both sentences went to a model that did not have the tool. Post 543's provenance shows it: a `writer.requested` run reading a paragraph telling it to make a call it could not make. The model's options are then to hallucinate the call, which comes back "Unknown tool", or to comply with a prompt it cannot comply with.

Nothing was going to catch this. The tool list was in `pipeline.py`, the prompt in `prompts.py`, the refusal sentence in `agent.py`, and the rule that ties them together was in a decision document. Three files, one invariant, no object holding it.

The same split had already produced a second defect nobody had noticed: `web_search` and `fetch_page` were **defined twice**, once in `agent.py` and once in `chat_tools.py`, with different descriptions and two copies of the UNTRUSTED frame that `chat_tools`' own docstring claimed was written once ("one phrasing, so a change is one change"). The assistant's copy dropped a page's outbound links, which is how the writer's prompt tells it to follow a "read more" to a primary source.

## The harness is the unit

One object per run: `name`, `system`, `role`, `max_steps`, `wall_clock_seconds`, `context`, tools, `on_idle`, `on_tool`, `closing`. It is a value built per run, not a constant, so budgets are read from `settings` at build time and stay live-configurable.

The whole point is that the flag that selects the tools is the same expression that fills the prompt:

```python
tools = ["web_search", "fetch_page", "finish_research"]
if allow_demote:
    tools.append("demote_story")
return build(
    name="writer" if allow_demote else "writer.requested",
    system=fill(WRITER_AGENT_SYSTEM, demote=WRITER_MAY_DEMOTE if allow_demote else WRITER_MUST_WRITE),
    tools=tools,
    ...
)
```

There is no second place to forget, because there is no second place. The budget-refusal sentence comes off the same flag and is stored on the context, so `ToolRefused` says "write from what you have" to a requested story and "write from what you have or call demote_story" to a normal one, from the one line that decided everything else.

Withholding the tool is still what enforces 0037. The model is simply no longer told about a door that is not there.

## Build time is a slot, mid-run is a message

The user drew this line and it is the reason `fill` and `offer` are separate mechanisms.

**A condition known at build time is interpolated into the prompt.** Slots are `{{name}}` rather than `{name}`, because several of these prompts contain JSON braces. A slot the caller does not fill raises, rather than reaching a model as literal `{{demote}}` text, which is the exact failure this module exists to prevent. An empty slot leaves no hole: after every slot is filled, runs of three or more newlines collapse to two and the result is stripped, once over the finished text rather than per slot, so it holds wherever the slot sits.

**A condition that arrives mid-run is a message.** The QA reviewer gains `rerender` the moment an edit introduces a chart or a diagram, and a model cannot be expected to notice a tool appearing in a table it has already read. `Harness.offer(name, because)` adds the tool and **returns the sentence that says so**, which the caller puts in the conversation. The return value is not decoration: a table that grows silently is a table nobody re-reads. Growing it also re-prefills the prompt, since tools render ahead of the messages, and that is the accepted trade — it fires only when an edit adds a figure, and a re-prefill is cheaper than publishing a blank one.

A terminal tool stays last through an `offer`, so the run's closing affordance is still the last thing the model reads.

## One registry, filled by whoever owns the handler

`llm/tools.py` holds the table; the research pair lives there because two stages share it, QA's editing tools stay beside the editor, and chat's stay beside its handlers. Each module calls `register`. A duplicate name is an import-time failure, which is what the two `web_search` entries could never have been.

A handler takes `(ctx, args)`. `Tool.context` declares the least `ToolContext` subclass it needs, and `build` refuses a harness whose context does not satisfy every tool it offers — a startup failure instead of an `AttributeError` three tool calls into a run. Budgets come off the context rather than `settings`, which is what lets one `web_search` serve a writer allowed six searches and a chat turn allowed two.

`Harness.tool(name)` returns None for a name the harness does not offer **even when it is in the registry**. That is what stops a model hallucinating another stage's tool into existence, and it is why tools are named rather than passed as objects.

## What this did not change

**The write gate is still structural, and now more so.** `agent._dispatch` raises `WriteProposed` above every handler for **every** harness, not only the assistant's, so a `writes=True` tool added to the writer or the QA reviewer still cannot execute itself. `chat_tools.execute_approved` remains the only other path to a write handler (0036). `tests/test_chat_tools.py` now iterates the whole registry rather than the assistant's slice of it, so that coverage arrives by construction.

**`WriteProposed` leaves the loop entirely** rather than being turned into a tool result. Nothing after it in the run is worth doing: the reader has to answer, and a turn is not held open across that. `chat.py` catches it as the loop unwinds and writes the proposal row.

**The assistant's open-article header is now a prompt slot.** It used to be concatenated onto the system prompt by `build_messages`, which was correct but was its own mechanism; a header is a condition known at build time, so it is a slot. That also keeps "exactly one system message, and it is index 0" true by construction, which matters because Qwopus's chat template raises `System message must be at the beginning` and llama-server reports it as a bare 400 naming no message.

**`on_tool` is the one thing only the assistant sets.** Somebody is watching the rail while a main-model tool loop runs behind a model swap, and minutes of silence read as a hang. It fires before the tool is looked up, so an unknown or a write-gated name is announced too: the reader saw the model reach for it either way.

## Verified

Live, 2026-08-29, against llama-server on 5001:

- A chat turn calling `search_posts`: the `tool` event arrived before the answer streamed, the shared handler ran on the assistant's budget, and the reply was saved (row 12).
- The write gate: the model called `write_article_from_url`, row 17 was written `pending` with a describe sentence, **no story was created and no job was deferred**. Rejecting it produced the `_REJECTED` tool result and a fresh turn on the resumed conversation (rows 17-20).
- Both writer harnesses, in the rebuilt worker: `writer` offers `demote_story` and names it in the prompt and in the refusal sentence; `writer.requested` does neither.

Deferred by the user: **enforcing that a prompt never names a tool the harness does not offer.** The registry makes it checkable — every tool name is a string in one table — and the bug this document is about is exactly what it would have caught. It is not built.
