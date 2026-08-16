# 0044. A page shows its own subject, and a long closed set folds

- Date: 2026-08-16
- Status: accepted
- Rule: `/admin/topics` shows the vocabulary. The job queue lives at `/admin/jobs` and is not duplicated onto other pages. The vocabulary table is **shut on arrival**, filtered **client-side**, and edited from **one box per row**.

## Context

`/admin/topics` carried a full copy of the job queue, polled every 10s, below a vocabulary table of **829 rows**. Everything the page is actually about — unclaimed labels, the bootstrap, the pending proposal — sat under that table, several screens down. Reaching the proposal meant scrolling past the entire vocabulary to get to it.

The duplication was also load-bearing in the wrong direction: the two bootstrap buttons targeted `#queue` with `hx-swap="innerHTML"`, so clicking "propose vocabulary" replaced the whole queue table with a one-line confirmation. Every other control in `/admin` reports into a `#defer-<task>` slot beside itself, which is what `admin_defer`'s docstring says it does, and these two were the exception.

## The queue is gone from that page

`/admin/jobs` is one click away in the sidebar and is the page whose subject it is. A second copy bought nothing and cost a 10s poll, a scroll, and the swap-target confusion above. The buttons now report into `#defer-propose_topics` and `#defer-apply_topics`, like everything else.

This is also what removed the 500 in 0043, at the root: the page no longer renders a fragment it has no context for, rather than being handed the context it should never have needed.

## The vocabulary folds, shut, with search outside the fold

The table is a **lookup surface**, not something read top to bottom — nobody reads 829 rows in order, they come looking for one. So `<details>` shut on arrival with the count in the summary, and the search box **outside** it: search is the way into a shut list, and putting the control inside would need the list open to reach the thing that opens it. Typing opens the fold.

Clearing the box deliberately does **not** close it again. The reader may have opened it by hand, and a fold that shuts itself under the cursor is worse than one left open.

Rejected: a height-capped scroll region (a nested scroll area is awkward with a wheel, and it is not a fold), and open-by-default with the state persisted in a cookie the way the queue's groups are (0033). That cookie exists because the queue is *polled* and the replayed fold animation was the flash; this table is swapped only on an edit, so it buys persistence and nothing else, and the first visit still scrolls far.

## Filtering is client-side, and so is the count

The vocabulary is a closed set already rendered in full, so a round trip per keystroke buys nothing. More to the point, rename and merge swap `#topic-rows` **wholesale** — both rewrite referencing rows, so every entry's use count can move — and a server-side query would have to be threaded back through every edit to survive one.

`app.js` owns the summary count for the same reason: a merge deletes a row from that tbody without re-rendering the summary above it, so a server-rendered number goes stale on the first edit. The server still stamps the initial one, which is what a reader without JS sees.

Matching is on a `data-search` attribute built in the template from label, slug and aliases, not on the rendered cell text. Reading the cells back would match whatever the row happens to display, which includes the "no embedding" badge and the placeholder text of the rename box.

## One box per row, and declared column shares

The row carried two boxes, one per verb, each 11ch, and they wrapped: **77px per row**, 11 rows to a screen. Both endpoints already read the same form field (`admin_topics_edit` takes `value` for either action), so the two boxes were never two arguments, only two copies of one. The row is now a single field the two buttons share, on one line: **41px**, 19 rows to a screen.

The form's own `hx-post` is **rename**, so a bare Enter takes the reversible action. Merge is a `type="button"` carrying its own `hx-post` and `hx-include="closest form"`, which keeps the browser from submitting the form underneath the htmx request.

The cost is a field with two meanings: rename wants a display label, merge wants an existing slug. That is stated in the card head and in the placeholder, and it is the trade taken deliberately - two boxes made the argument obvious and the table unreadable.

Column widths are declared (`table-layout: fixed`, 20/6/*/34) because auto layout spent **390px** on a ten-character mono label while the alias chips wrapped to three lines in what was left. They are percentages so the 62rem breakpoint shrinks the columns rather than overflowing the card; below it the box drops to its own line, since 34% of a narrow viewport left it 50px. The "no embedding" badge lost its column and moved into the label cell: it is set on a handful of rows, and an almost-always-empty column is paid for by all 829.

## What a failed edit still does

Nothing visible. A rename or merge that 4xxs targets `#topic-rows`, which is not a navigation outlet, so `errors._render` does not mark it `HX-Error-Page` and htmx discards the body (0043). The row simply does not change. That is the correct behaviour for the *poll* the opt-in was designed around, and the wrong behaviour here, but the fix is an inline per-row error slot rather than swapping an error document into a `<tbody>`. Left undone and recorded.

## Verified live

2026-08-16, at 2560px, against the real container and its 829-entry vocabulary.

- The whole page is now **one viewport**: `scrollHeight` 1400 against a 1400px window, from several screens.
- `deep-sea` → "3 of 829 topics", fold auto-opened, matching on label, slug and alias. `ASTRONOMY` → 7, so case folds. A miss → "0 of 829", the no-match line, rows all hidden.
- A simulated `#topic-rows` swap (the rename/merge path) with two rows dropped while `astronomy` was filtered: "7 of 829" → "**6 of 827**", so the filter re-applies and the total is recomputed from the DOM rather than trusted from the server's first render.
- Reached by a boosted sidebar click as well as a direct hit; no console errors or warnings.
- **Not** clicked: "propose vocabulary", which would enqueue a real clustering job over all 829 labels. Its slot resolves, which is what the old `#queue` target no longer would.

The one-box row, same day, same container:

- A real **rename** through the shared field: `survey research` filtered to one row, its box set to its own current label, rename clicked. `POST /admin/topics/survey-research/rename → 200`, the tbody swapped all 829 rows, the filter re-applied to "1 of 829", label and use count unchanged. A no-op by value, the whole path by mechanism.
- A real **merge** click with `no-such-topic-xyz` in the same box: the confirm named the right topic, and `POST /admin/topics/survey-research/merge → 404`. The 404 is the proof, not a failure: it comes from `topics.merge`'s `LookupError`, which is reached only *after* the route accepted a non-empty `value`. An empty field would have been the route's own `400 value is required`, so 404-not-400 is what says `hx-include` carried the box. `topics.merge` looks both entries up before it writes anything, so nothing was mutated.
- Column widths at 2560px: 237 / 71 / 474 / 403, row height 41px against the old 77. At 868px: no horizontal overflow (`scrollWidth == clientWidth`), box on its own line at 246px.
