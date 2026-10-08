# 0034. Logs stream as byte-offset deltas, and only the container-to-host leg polls

- Date: 2026-08-03
- Status: the llama.cpp log pane it served was removed by [0060](0060-the-warden-client-reads-infermux.md), 2026-10-08; InferMux shows its logs in its own UI.
- Rule: `since=<byte offset>` in, `next_offset` out. The browser is pushed to.

## Context

From TODO "llm log fetches the full log instead of streaming or appending. prefer streaming." The pane re-fetched its entire 300-line tail every 3s over htmx and swapped it in.

The bytes were the small part. A swap **destroys the reader's text selection and resets their scrollback**, every three seconds, while they are reading the thing they opened the page for.

## Decision

One protocol, two consumers. The host agent's `/logs` gained `since=<byte offset>` and returns `next_offset`: the bytes written after that offset, nothing else. `GET /api/llm/logs?since=` exposes the same delta for scripts, and `GET /api/llm/logs/stream` is the pane's transport.

SSE event names are `reset` (replace), `lines` (append) and `unavailable` (keep trying). Deliberately **not** `error`: SSE dispatches a server-sent `error` onto the same handler as EventSource's own transport error, distinguishable only by whether `data` is present.

**The browser is pushed to; only the container-to-host leg polls**, at 1s (`llm_log_stream_interval_seconds`). A `tail -f` held open across the Docker boundary would put a long-lived connection on the optional, restartable host agent and give it a tailing loop in a threadpool worker, to save a delta read that is one file seek. The offset makes that leg stateless and idempotent.

## Three things the offset has to get right

- A **trailing partial line** is withheld and `next_offset` stops short of it, or the rest of that line arrives later as a line of its own.
- A **shrunk file** answers `reset`. The launcher truncates on every start, so any offset held across a restart points into a different file.
- A **backlog past the window** also answers `reset`, with `gap_bytes` measured, so the pane can admit the discontinuity rather than splice two distant parts of the log. A seek from the end still drops its own partial head.

## Version skew is a normal state here

The agent is on the host and the app is in a container, so they update separately. An agent with no `next_offset` in its answer degrades to replacing, which is exactly the old poll. The fragment omits `since` entirely rather than emitting `?since=`, because an **empty** parameter is a 422, which an EventSource then retries forever. That 422 was observed live from a neighbouring session before the fix.

## `read_log` is the function; `logs` is the route

A route's defaults are `Query` objects, not values, so in-process callers such as the tests got a `Query` wherever they omitted an argument. Harmless until `since` started doing arithmetic. Splitting them is what made the delta testable at all. (Same trap as 0032, from the other direction.)

## Client side

The fragment renders a snapshot **once** and hands the stream its `next_offset`, so the handover re-sends nothing and the pane never flashes. That snapshot is also the whole no-JavaScript story.

`app.js` appends each delta as its own text node, never rewriting an existing one, which is what preserves a selection spanning them; trims by dropping whole leading nodes past 5000 lines; and tracks the pane **element** rather than a boolean, so `htmx:load` is idempotent: same element, do nothing; gone or replaced, close the old EventSource first, since an htmx swap discards the node without telling us.

## Verified live, 2026-08-03, against the real llama-server logs

A 1.49 MB `router.log` tail read, then an embedding call: the delta returned exactly the 3 lines it produced and the offset advanced. Through the app, the stream resumed at `since=6716` and emitted three `lines` events (7396, 7617, 7736 bytes) as the server wrote, plus `: keep-alive` after 15s of quiet, under `text/event-stream` and `no-store`.

In the browser, the fragment was fetched twice in the whole session, on load and on a log switch, and **never polled**. The pane grew 78 to 83 lines with everything above byte-identical, **a selection made before the append survived it**, switching router to embed closed the old stream and opened exactly one new one, the size readout stayed live, scrollback stayed pinned, and there were zero console errors and zero 422s.
