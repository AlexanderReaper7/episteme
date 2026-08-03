# 0033. A polling fragment answers 204 when nothing changed, and the server stamps its open state

- Date: 2026-08-03
- Status: accepted
- Rule: a poll URL carries a digest of the **data**. Equal digest means 204, which htmx does not swap. The client records disclosure state; it never applies it.

## Context

Reported against the redesigned `/admin/jobs`: the poll was flashing, moving the scroll, and re-sending everything.

Measured: every 10s tick transferred **1385 B gzipped, 14 833 B raw, of byte-identical HTML** and replaced the DOM with a copy of itself.

## Three causes, three fixes

**1. The flash was the fold, and it was self-inflicted.** The server rendered `<details>` closed and `app.js` set `.open = true` afterwards from localStorage, replaying the 0.26s `block-size: 0 → auto` animation on **every poll**, and the height change under the cursor was the scroll jump.

The remembered state became a **cookie**, session-scoped to `/admin`, that the **server** stamps. `app.js` only ever *records*, never applies. A fragment therefore arrives in its final shape with nothing left to animate. This also killed the identical flash on a cold page load, which localStorage could never reach, because it runs after first paint by construction.

**2. An unchanged fragment answers 204.** A digest of the data rides in the fragment's own poll URL (`?view=…&v=…`), so the next request tells the server what the browser is showing. Equal means `204`, which htmx does not swap. That is what actually removes the churn: the cheapest possible re-render is still a re-render.

`Cache-Control: no-store`, because the same URL legitimately answers 204 now and 200 once the state moves past its `v`. A missing or empty `v` always gets content, since a pre-mechanism fragment or a hand-typed URL must not receive a 204 it would render as an empty panel.

**3. The digest covers data, not rendered text.** Otherwise "2m ago" becoming "3m ago" would flip it every minute with nothing having happened, and the churn would survive the fix. Ages ship as `<time datetime=… data-ago>` and `app.js:retimeAgo` refreshes the text in place every 30s: no network, no swap, touching only text nodes. The server still renders the words, so a JavaScript-less reader gets a correct if frozen age. `agoText` mirrors `_ago`'s thresholds, duplicated on purpose since one is SSR and one is a ticker, and pinned by a test that reads both.

## Open state is deliberately not in the digest

It is per-reader and already in the browser, so folding it in would make one reader's click re-render on the next tick for nothing.

## Consequences

Two clocks, deliberately: the queue table and the log pane poll separately, because they change for different reasons.

Verified live: steady state **204 / 0 bytes**, first paint 3027 B gzipped for 200 jobs. Across 25s and two ticks, **zero node replacements, zero height changes, zero scroll movement**, and one characterData mutation, which is the 30s retimer. A real defer still swapped within one tick, since the digest moved, so the 204 path does not freeze the page. A forced swap with a stale digest returned an opened group **already open** from the server, and only that one.
