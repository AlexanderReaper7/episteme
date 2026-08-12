# 0038. Chat streams over SSE, on a fourth gateway role, through the loop the writer already uses

- Date: 2026-08-11
- Status: accepted
- Rule: `chat` is a role like any other (0003). Streaming enters `run_tool_loop` through an injected `turn`, never a second loop.

## Context

Every LLM call in Episteme was initiated by the machine. Adding a human at the other end changes exactly one thing about the requirements and it is not the model: **someone is watching**. A tool loop on the main model, possibly behind a 100-second model swap, is minutes of blank screen otherwise.

Three sub-decisions fell out of that: how bytes reach the browser, which model answers, and whether the driving loop is the writer's or a new one.

## Decision

### SSE, not WebSocket

The client sends exactly two things: a message, and an approve/reject. Both are discrete, idempotent, auditable actions that want to be ordinary POSTs — in the access log, in a test, in `curl`. Nothing travels client→server *while* tokens flow.

Against that, WebSocket costs automatic reconnect, `Last-Event-ID` resume, a `CompressMiddleware` interaction, and a connection lifecycle that has to be reconciled with htmx swaps by hand.

`POST /chat/turn` **itself** returns `text/event-stream`, read with `fetch()` + `ReadableStream` + `TextDecoder`. No `EventSource`, because `EventSource` is GET-only and would force a two-step "post the message, get a turn id, open a stream on it" dance whose only purpose is to satisfy the API's shape.

Frames carry the type in the SSE **event name**, so the client dispatches on `name` and nothing sniffs the payload. Six events: `text`, `tool`, `saved`, `proposal`, `resolved`, `error`, plus a terminal `done`.

### Rendering stays on the server

The stream carries text, not markup. The panel appends raw deltas into a `pre-wrap` bubble, then swaps in the server's rendered Markdown from `/chat/message/{id}` on `saved`. A Markdown renderer in the browser would be a second rendering path for LLM prose whose first path is already sanitized through nh3 — two places to keep in agreement, one of which is reachable by a model's output.

Deltas are appended as text **nodes**, not by rewriting `textContent`: a rewrite collapses any selection the reader has made, on every single token.

### A fourth role, defaulting to main's model and server

0003 says ask for a role, never a model or a URL, so chat is `Role = Literal["main", "fast", "embed", "chat"]` and two settings in the house `""`-means-inherit idiom: `llm_chat_base_url` → `llm_main_base_url` → `llm_base_url`, and `llm_model_chat` → `llm_model_main`.

Sharing main's server is the default because chat and the writer want the same reasoning quality, and because sharing means an interactive turn never triggers a model swap while `write` is running. `endpoints()` groups by URL, so chat collapses into main's row for free.

**The honest cost:** a chat turn during the `write` stage queues behind the running generation, seconds to a minute. A chat turn while `triage` or `condense` hold the *fast* model forces a full swap, roughly 100 seconds each way, stalling the pipeline. That is the price of not budgeting a third resident model on 10GB. The interactive lease (0037) stops it becoming worse than slow.

### One loop, with a seam

`run_tool_loop` is already the driver a chat agent needs: budgets, dispatch, the idle nudge, the stuck-loop breaker, the terminal-tool exit. The only line that would differ is how a turn is taken, so that line became a parameter:

```python
Turn = Callable[[list[dict], list[dict]], Awaitable[dict]]
...
if turn is not None:
    message = await turn(messages, tools)
else:
    message = await gateway.chat_messages(role, messages, tools=tools)
```

The chat agent passes a `turn` that consumes `gateway.chat_stream`, pushes text deltas onto an `asyncio.Queue` the SSE generator drains, and returns the assembled message. The writer and QA pass nothing.

`chat_stream` yields `{"type": "text", "delta": …}` as tokens arrive and finally exactly one `{"type": "message", "message": {…}}`, **identical in shape to what `chat_messages` returns** — a terminal event rather than a return value, because a bare async generator has no return an `async for` can see. That identity is what keeps everything downstream ignorant of streaming.

The fiddly part is tool-call reassembly: OpenAI-compatible streams deliver `tool_calls[i].function.arguments` as string fragments across chunks, keyed by `index`. `StreamAccumulator` collects them into one message. Observability is unchanged: one `record_llm_call` at the end, from the assembled message, so a streamed turn and a blocking one are the same `llm_calls` row, and `stream_options.include_usage` is set because a streamed response carries no usage block otherwise.

The equivalence is tested by running one script through the loop **both ways** and comparing the transcripts, so a future change that quietly special-cases the injected path is a diff rather than a surprise in the browser.

## Rejected

- **A `chat_agent.py` with its own loop.** Forty duplicated lines whose divergence would be discovered as "why does chat not nudge".
- **`EventSource`.** GET-only; costs an id round trip to carry a message body.
- **A third resident model for chat.** 10GB, and the GPU is shared with games. Revisit if contention proves annoying in practice; it is a launcher and VRAM decision, not a code change here.
- **Markdown in the browser.** Two sanitization stories, one of them fed by a model.
- **A cookie or `localStorage` conversation id.** Single-user forever; the server already knows. A server-side id in `app_state` survives a reload and works from a second browser.

## Consequences

- One conversation at a time. Deliberate, and a second one is a row's worth of work if it is ever wanted.
- The rail is fetched as its own fragment on load, so the conversation's state never enters a page's ETag (0031) and `_post_page_etag` is untouched.
- The rail lives outside `<main>`, so boosted navigation cannot destroy it — the post id is therefore re-read from `location.pathname` on every turn rather than captured when the panel was built.
- A dropped connection cancels the work: `_pump` runs the turn as a task and cancels it in `finally`. The assistant row is only written when the turn completes.
