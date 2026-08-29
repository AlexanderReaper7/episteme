# 0047. A post stores its destination, and a body means it renders itself

- Date: 2026-08-28
- Status: built and live-verified 2026-08-28
- Rule: `posts.href` holds where a card and the canonical URL both go. NULL means the post renders itself at `/post/{id}`. A database CHECK ties the two together: `(href IS NULL) = (sections is a non-empty array)`. A story's primary item is its earliest `published_at`, then its lowest `id`.

## Context

`_feed.html` hardcodes `/post/{{ post.id }}` in both branches, at line 13 for an aggregate card and line 49 for an article. So reaching an aggregated article takes two clicks, through a page whose only content is a relisting of the cluster the card already showed. The destination should be the primary item's URL.

Fixing that one template line raises the general question, because `/post/{id}` is still the post's identity: search returns it, provenance hangs off it, the assistant links it. The answer that avoids a special case is that **`/post/{id}` resolves to wherever the post's content lives**, which for an article is the page itself, for an aggregate is its source, and for a filed post is its correspondent's page (0046). One rule, one derivation, applied to both the card and the canonical URL so the two cannot disagree.

## The body invariant

From that, a property nobody had written down: **a post carries a body only if it renders itself.** Not a rule about kinds. `sections` is non-empty exactly when `href IS NULL`.

The reason to trust it is that it already describes every row in the database:

```
 kind      | status    | jsonb_typeof | has_body | count
 aggregate | published | array        | f        |   473
 article   | archived  | array        | t        |     3
 article   | published | array        | t        |   170
```

Articles render themselves and have sections. Aggregates redirect and have none. A filed post will redirect and have none. `sections` is `NOT NULL` and is a JSON array in all 646 rows, so the constraint has nothing to fix up first:

```sql
ALTER TABLE posts ADD CONSTRAINT ck_posts_body_iff_self_rendering
  CHECK ((href IS NULL) = (jsonb_typeof(sections) = 'array' AND jsonb_array_length(sections) > 0));
```

The `jsonb_typeof` guard is not decoration. `jsonb_array_length` raises on a JSON scalar rather than returning false, so without it a stray `'null'::jsonb` makes the constraint *error* instead of reject, and an erroring constraint is harder to diagnose than a failing one.

## Stored, not derived

Deriving `href` in Python would have cost nothing in joins. The aggregate branch of `_feed.html` already reads `primary.source.name` at line 32, so `story.items` and `item.source` are in the session by the time a card renders, and an article needs only `post.id`.

Storing it was chosen anyway, for the constraint. A derived `href` cannot be checked by the database at all: a CHECK cannot join to `source_items` to learn what an aggregate's destination would be, and a CHECK written over `kind` instead would restate the resolver's mapping in SQL and drift from it. Storing turns the invariant from a test that runs in CI into a rule the database enforces on every write.

`banner_url` (`models.py:128-131`) is the same move made before, for a different reason: "the derivation isn't cheap, so store the minimum". Here derivation *is* cheap and the argument is enforcement instead.

## What storing costs, measured

A stored `href` goes stale if an aggregate's primary item changes. It can: `cluster_items` matches each pending item against `Story.last_item_at >= cutoff, Story.centroid.is_not(None)` (`pipeline.py:120`) with **no filter on story status**, so a story that already has a published aggregate post keeps absorbing items for the rest of its 5-day window.

How often that actually moves the primary, against the live database:

```
aggregate_posts              473
grew_after_the_post_existed    9
primary_would_have_changed     0
```

Nine clusters grew after their post existed. None of them changed primary, because `cluster_pending()` processes oldest-published first (`pipeline.py:107`) and a later-arriving item almost always has a later publish date. The drift needs a lagging feed, a source added mid-window, or a backfill.

So the repair is insurance, not a fix for an observed bug, and it is written anyway: `cluster_items` already loads and mutates the story four lines above, the guard only fires when an arriving item sorts ahead of the current primary, and the failure it prevents is a link that quietly points at the wrong article with nothing to detect it.

`banner_url` has the identical staleness, also measured at zero in the same window. It is left alone.

## The primary item

`Story.items` at `models.py:103` has no `order_by`, so `story.items | first` is whatever Postgres returned, which is physical row order and not a guarantee. That already picks the aggregate card's title, its source name and its snippet, so two renders of one card could in principle disagree. Making it pick the destination as well turns a latent inconsistency into a wrong link.

The rule is **earliest `published_at`, then `id`**, set as `order_by` on the relationship so the title, the source line, the snippet and the href cannot point at different items. No item has a NULL `published_at` in the current data, so the ordering is total.

Ordering by `source.credibility_rating` instead was the obvious editorial alternative and is measurably not a choice yet: **`sources` holds one distinct `credibility_rating`, 0.5, across every row.** Nobody has ever tuned it, so a credibility ordering degenerates entirely into its own tiebreak, which is this rule with an extra join into `sources` at every call site. If credibility is ever tuned it becomes a genuinely different and more drift-prone rule, since a strong outlet covering a story a day late is ordinary, and that is the moment to revisit it with its own measurement.

## Consequences

- **12 of the 473 aggregate cards change primary item** at backfill, from the current accidental id-order to published-order. They change title, source name, snippet and destination. This is a one-time correction of cards that were already choosing arbitrarily, and is not the same number as the drift figure above, which is 0.
- **The constraint starts rejecting writes that currently succeed.** An article generated with zero sections is a broken post today and lands silently as an empty page; afterwards it is an `IntegrityError` at insert. That is the right behaviour, and the writer's error path has never had to handle a rejected insert.
- The migration adds the column, backfills every aggregate from its primary item, and only then adds the CHECK. The backfill is exactly what the review rule means by "what the diff cannot see", so the revision is written by hand rather than trusted from `--autogenerate`. No aggregate has an item-less story (measured: 0), so the backfill cannot produce a NULL that the constraint then rejects.
- A search hit on an aggregate now leaves Episteme on click, since `/api/posts/search` returns post ids the assistant links directly.
- `/post/{id}/provenance` becomes reachable only from the card's `card-provenance` link (`_feed.html:30`), because the page that used to carry it no longer renders. That link stops being a convenience and becomes the only door.

## What was built, and what was watched

Landed 2026-08-28 as revision `499afada00fa`. `Story.items` carries `PRIMARY_ITEM_ORDER`, defined once in `models.py` and reused by `pipeline._primary_item`, so the SQL that picks a card's title and the SQL that picks its destination are the same expression. `models.POST_KINDS` records, per kind, whether it renders itself, and `Post.kind` refuses a value that is not in it: a kind cannot reach the database without someone saying where it points. The aggregate branch of `post.html` and `_stories.html` are deleted, along with the story-items half of `_post_page_etag` and the two CSS blocks only that template used.

An htmx wrinkle the design did not anticipate: a boosted click arrives as a `fetch`, and a fetch that follows a **cross-origin** redirect fails on CORS with nothing shown. htmx does not boost cross-origin anchors, so the feed cards are safe on their own, but `/post/{id}` is still linked from search and the assistant. It answers `204` with `HX-Redirect` to an `HX-Request`, and `302` to anything else.

Watched live against the compose stack, not merely unit-tested:

- The backfill: 473 of 473 aggregates carry an href, 173 of 173 articles carry NULL.
- The constraint rejects **both** directions. An article inserted with `sections = '[]'` and an aggregate inserted with both an href and a body each come back as `ck_posts_body_iff_self_rendering`.
- The feed serves external URLs on aggregate cards and `/post/{id}` on article cards, in one page.
- `/post/646` answers `302` to space.com; the same URL with `HX-Request: true` answers `204` with the same target in `HX-Redirect`; `/post/652` (an article) still renders 200; `/post/646/provenance` still answers 200; an unknown id still 404s.
- `ensure_aggregate_post` and `_rehome_aggregate_card` run inside the worker container against the real database, in a transaction rolled back afterwards: a two-item story's card takes the OLDER item's URL, an item arriving with an earlier publish date re-points it, an item arriving later leaves it alone, and setting `sections` on that aggregate raises `IntegrityError`.

Not watched: any of it in a browser, and no aggregate has been minted by a real triage pass since (that needs a pipeline run).
