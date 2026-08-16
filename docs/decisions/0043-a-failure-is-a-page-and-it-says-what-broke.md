# 0043. A failure is a page, and it says what broke

- Date: 2026-08-16
- Status: accepted
- Rule: an HTML request that fails renders `error.html` with the **full traceback**. `/api` keeps its JSON body byte for byte. A boosted navigation gets the page as a swappable fragment, because a fragment htmx discards is the same as no page at all.

## Context

`/admin/topics` returned `Internal Server Error` — five words, no status detail, nothing to act on. The actual failure was `jinja2.exceptions.UndefinedError: 'retention' is undefined` at `admin/_admin_queue.html:111`, and reading that took a `docker compose logs web` and a scroll through 32 frames of ASGI plumbing.

Worse, the page is normally reached by clicking "Topics" in the admin sidebar, which is a boosted navigation. htmx does not swap a 5xx body, so that click did **nothing at all**: no error, no navigation, no console message a reader would see. The failure was not merely unhelpful, it was invisible.

## Full tracebacks go to the browser

Single-user forever, no auth (spec §1), reached over a tailnet. There is no audience to withhold a traceback from, so the usual production/debug split would be mechanism guarding against a reader who does not exist. No `debug` setting, deliberately: a knob whose only correct value is `on` is a knob that will one day be `off` during the incident it was built for.

Locals are **not** captured. That is not a privacy line, it is a legibility one: `capture_locals=True` puts a repr of the whole request context into a frame that already tells the truth with its source line.

## The page's shape

Three things, in decreasing urgency:

1. the status, the exception line, and the **innermost frame that is ours**. For a template error that frame is the template line, which for the failure above is the whole answer and is four lines from the top of the page;
2. the request that produced it, including the `HX-*` headers, since "which navigation was this" is otherwise unrecoverable from a fragment;
3. every frame with its source line, with app frames given an accent rail and dependency frames dimmed, then the raw traceback text, collapsed, for pasting.

The frame list is **this exception's own stack**, not the chain. A `raise HTTPException(422, ...) from exc` would otherwise bury the frames that matter under the two that re-raised. The chain is in the raw text, which is where a cause belongs.

## `/api` is untouched

`detail` is a contract that other code reads, so the JSON handlers delegate to FastAPI's own (`http_exception_handler`, `request_validation_exception_handler`) rather than reimplementing them. The audience split is keyed off the **path**, not `Accept`: the browser sends `Accept: */*` for a `fetch` and htmx sends it for every swap, so a header that cannot separate the two cannot decide this.

## Swappability is opt-in, per response

The server sets `HX-Error-Page` only when the failing request targeted a navigation outlet (`main-content`, `admin-main`), and `app.js` forces `shouldSwap` on exactly those. The alternative — a global `htmx.config.responseHandling` rule that swaps every 4xx/5xx — would let a **failing background poll** (the job queue, every 10s; the log pane) replace the page the reader is on. One transient 500 from a poll should not cost the reader their scroll position, let alone their page.

Errors always retarget to `#main-content`, because `error.html` has a `content` block and no `admin_content` one: an intra-admin navigation that fails replaces the admin shell rather than half-filling it.

## The page cannot be the thing that fails

`error.html` extends `base.html` — a failure is still a page, and the header and chat rail are the way out of one — which means a break in `base.html` or the generated icon sprite would take the error page down with it. `errors._render` therefore catches its own render failure and emits a self-contained document with no template, no CSS and no context, carrying **both** tracebacks: the original, and the one that hid it.

## The bug that prompted it

`/admin/topics` embedded the queue, and `_admin_queue.html` renders from `queue_context` — the same builder `/admin/jobs` and the polled partial use. The topics route passed a hand-rolled `jobs` list instead, so the page 500'd on the first key the shared fragment needed and the route did not have.

The queue was then removed from that page outright rather than given the context (0044), which is the real fix: the page was rendering a second copy of another page's subject. `admin_topics_discard` still renders through `admin_topics` rather than re-assembling the context itself, since a second caller assembling it by hand is how this happened.

## Verified live

2026-08-16, against the real container, at 2560px.

- The bug reinstated: `/admin/topics` direct hit renders the page, origin `episteme/web/templates/admin/_admin_queue.html:111` with its source line, 32 frames, app frames railed.
- The same failure reached by clicking the sidebar: swapped into `#main-content`, URL pushed, tab title `Episteme • 500 Internal Server Error`, header and chat rail intact.
- `/post/999999`: 404, 5 frames, Route `/post/{post_id}` and path params listed.
- `/api/posts/999999` → `{"detail":"Not Found"}`, `/api/jobs?limit=nonsense` → FastAPI's own 422 list. Both unchanged.
- Fix restored: `/admin/topics` 200.
