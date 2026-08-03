# 0031. One predicate decides both the response body and its cache validator

- Date: 2026-08-03
- Status: accepted
- Rule: `templating.fragment_block(request, template)` is the single answer to "what is this response". Every ETag derives from it. `Vary` names every header it reads.

## Context

Reported as "clicking Episteme in the top bar now nests the page", with a saved page showing two headers, two sprites and two `#main-content` elements.

One URL, two bodies, **one cache key and one ETag**.

## Root cause

Two places decided what the response was. `templating.render` chose the body from `HX-Target`, returning the `content` block for `#main-content`, while `web/app.py` computed the ETag and `Vary` from `HX-Request` alone.

The case between them is real. htmx's Back/Forward restore (`loadHistoryFromServer`) sends `HX-Request: true` with **no** `HX-Target`, because it wants the entire document. It got one, stamped with the *fragment's* ETag, inside the same `Vary: HX-Request` cache entry.

Measured before the fix: an 85 424-byte document and a 71 107-byte fragment both answering to `W/"cb5ab677…"`. The next boosted click on that URL then revalidated into a 304, or a `stale-while-revalidate` hit, and htmx swapped a whole document, sprite and header and nav and `#main-content`, into `#main-content`.

Standards-compliant caching doing exactly what it was told. Nothing browser-specific, though it needs a Back/Forward first, which is why it appeared in the user's daily Firefox and not in a fresh automated session.

## Decision

`templating.fragment_block(request, template)` returns the block the body will be, with None meaning the whole document. `render` and all three validators (`_feed_etag`, `_post_page_etag`, `_provenance_etag`) derive from that one answer, so an ETag can never name a body the server did not send.

The representation key stopped being a bool (`fragment`) and became the block name, which is what actually varies. `Vary` is now `HX-Request, HX-Target`, both headers the predicate reads.

`fragment_block` also checks up front that the **leaf** template defines the block, and `title`, rather than catching `BlockNotFoundError` mid-render. That fallback to a full document was the other way the body and its validator could disagree.

## Verified live, 2026-08-03, in the browser

The sequence, a history-restore-shaped request then a click on the logo, reproduced the user's saved page exactly: 2 headers, 2 sprites, 2 `#main-content`. After the fix the same sequence gives 1 / 1 / 1. The two full-document representations are byte-identical under one ETag while the fragment has its own. A real Back then Forward with htmx's `htmx-history-cache` cleared, so the restore must hit the server, leaves the page intact. `/post/{id}` shows the same clean three-way split, and intra-admin navigation still swaps `#admin-main` with the sidebar persisting.

Regression tests in `test_caching.py` (three representations, never a shared validator) and `test_htmx.py` (a history-restore request gets the whole document).

## Unrelated, fixed alongside

Three vendored jsDelivr bundles carried a trailing `//# sourceMappingURL=/sm/<hash>.map`, an **absolute** path, so devtools resolved it against our origin and logged a 404 on every page load. Stripped, with the step added to the vendor README's re-download recipe.
