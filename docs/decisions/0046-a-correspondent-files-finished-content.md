# 0046. A correspondent files finished content, and owns a page for what stays true

- Date: 2026-08-28
- Status: accepted; filing, scheduling, registration, routes and Glance built 2026-08-28. Matsedel and the filed feed card are not
- Rule: A correspondent produces finished posts on its own schedule and **files** them, skipping triage and the writer. Its episodic content enters the feed as ordinary posts; its standing content lives on its own page under `/c/<slug>/` and never enters the feed. A plugin correspondent ships inside this repository and runs in-process. An external one keeps its own database, pushes posts over an authenticated endpoint, and serves its standing content on request.

## Context

The first correspondent is **Matsedel**, which reads the weekly lunch menus of four restaurants. It fits nothing in the existing pipeline. There is no cluster, because four menus are four documents that never merge. There is nothing to triage, because the reader has already decided a lunch menu is worth a card. There is nothing for the writer to write, because a menu is a list of dishes and generating prose around it would be the filler spec §1 forbids. And the content is not news: this week's menu stays true until next week.

Ingestion cannot express that. `ingest/registry.py` adapters produce `SourceItem`s that go on to be embedded, clustered, triaged and written. A correspondent produces the *output* of that chain directly.

## Episodic and standing

The distinction that makes the rest fall out:

- **Episodic** content is read once and then done with. The feed is entirely episodic, and the promise that a reader can be caught up depends on there being a finite amount of it.
- **Standing** content stays true until it changes, and is looked up rather than caught up on. This week's lunch menu is standing. Today's menu is episodic.

Today's menu is a post in the feed. The week lives on Matsedel's own page, which is where the post's card takes you.

## Filing

Filing is both the act and the entry point: a correspondent hands over a finished post rather than raw material for the pipeline to generate one from. `ingest/manual.py` is the precedent, minting a `Story` and a `SourceItem` outside the normal path so a reader-requested URL has somewhere to live.

Three things follow:

- `stories.status` gains **`filed`**. A correspondent's story has `item_count > 0` and would otherwise be a live candidate for triage and the writer. Reusing `triaged` with a fabricated `triage_decision` was rejected: nothing triaged it, and the record would say otherwise.
- **One story per period.** Each day's menu is its own story carrying its own items. An earlier design had the correspondent filing repeatedly into one long-lived *standing story*, so each post superseded the last under the existing rule of at most one published post per story. Scheduled publication killed it: five weekday posts created on Sunday all exist at once and cannot supersede each other. Rewriting the invariant as "at most one *visible* post per story" was rejected, because the write path, QA demotion and the archive prune all read it, and every future reader of that rule would have to know about lunch. Stories are cheap; 260 a year is nothing beside 473 aggregates a month, and `story_id` stays NOT NULL either way.
- `source_items.hash` widens its contract from `sha256(canonical url)` to `sha256` of a **producer-chosen identity**. Matsedel's is `Matsedel/koppargrillen/2026w35`: correspondent, site, period. A correspondent re-reading one unchanging URL every week cannot key on the URL, because the column is unique and `canonicalize_url` drops fragments (`ingest/base.py:29`). The column stays a digest and keeps its name; only what goes into it is now the producer's choice. Ingestion keeps hashing the canonical URL and `content_hash` itself does not change, since article dedup depends on two feed entries for one URL colliding.

A filed story must carry at least one real item with a URL. That is what keeps `item_count > 0` honest and what makes the post accountable through `/post/{id}/provenance`. A correspondent that reads nothing is generating content, which is the writer's job. Filed items are embedded like any other, which costs one call to the 0.6B embedder per period and is what makes the week's menu reachable from the assistant's semantic search.

## Scheduling and the shape of a lunch post

One post per weekday, carrying that day's menu across all four restaurants. One post per restaurant would put four cards in the feed every morning and turn the correspondent into a source of volume; one post per week would make the feed carry standing content, which is the thing the feed is not for. On a day when no restaurant has a menu, nothing is filed rather than an empty card.

**Fetching and publishing run on different clocks.** Each site is read once a week and the whole week is stored; the five weekday posts are created from that single read, each carrying a `publish_at`. The menus are weekly documents, so a daily scrape is four times the requests for the same bytes, and politeness is a hard requirement rather than a preference (0005). It also means a site being down on Wednesday morning costs nothing, because Wednesday's post came from Monday's read.

Publishing ahead rather than minting nightly is what **decouples lunch from the pipeline's health**. The alternative, minting each day's post during the 03:00 run, means no lunch card on any night the pipeline is paused, the governor is holding the GPU, or a job failed. Lunch needs no LLM at all, so making it depend on a nightly LLM run would have been backwards.

The mechanism is a `posts.publish_at` column and a feed filter, `publish_at IS NULL OR publish_at <= now()`. No job, no extra status, nothing that can fail to fire. Two things it touches:

- **`_feed_sort_at` must sort on it.** `web/app.py:79-84` sorts an article on `generated_at`, so five posts created on Sunday would all carry Sunday's timestamp and Friday's card would arrive five days stale. Both the SQL expression and its Python mirror change, and `test_scoring` pins them together.
- **Nothing archives a lunch post afterwards.** It stays `published`, sinks by freshness, and is never pruned, since the prune at `pipeline.py:855` only touches `archived`. That costs rows, roughly 260 a year, and no work: the kind filter added to `score_pending()` keeps them out of every stage. A nightly maintenance sweep and self-archiving by the correspondent were both considered and both add a moving part to save rows that are already free.

**Expiry is the same mechanism from the other end** (decided 2026-08-28). A lunch menu is worth reading until the kitchen closes and is purely historical after, so `posts.expires_at` mirrors `publish_at` exactly: one nullable column, one clause, `expires_at IS NULL OR expires_at > now()`. NULL is no bound, which is every post that is not filed. The two boundary conditions are deliberately not symmetric - a post due exactly now is in the feed, one expiring exactly now is out - so the window is half-open and a post cannot be both due and expired at one instant. Expiry removes a post from the **feed** and nothing else: it keeps its own URL, and the week it belongs to is still on the correspondent's page, which is where standing content lives. Nothing archives it, for the same reason nothing archives it at any other time. The alternatives were a nightly sweep flipping status, which adds a job that can fail to fire for content whose whole point is that it needs no job, and rendering an expired card greyed out, which keeps a card the reader cannot act on in a stream that has no room for it.

A filed post carries no affinity score, and `_rank_expr` already does `func.coalesce(Post.affinity_score, 0.0)` (`web/app.py:128`), so it ranks as a neutral post of its age rather than propagating a NULL through the sort. That is left as is. A fixed affinity to keep lunch high would be a thumb on the scale in a feed whose whole premise is that ranking comes from feedback.

## Plugin or external, and what that decides

A plugin correspondent ships in this repository and runs in-process. An external one is a separate application. Almost every consequence follows from that split, because two capabilities exist only in-process:

**Politeness.** Anything that fetches from someone else's site must run in-process, because only in-process code can be held to `ingest/http.polite_get` (0005, 0006). Matsedel scrapes, so Matsedel is a plugin. This is not a preference, and it is what makes the external path speculative rather than urgent: the class currently has no members.

**Storage.** A plugin may define its own tables. An external service cannot ship a migration, and the alternative, an outside application issuing DDL through an API, is strictly worse than a plugin doing it. So an external service keeps its own database and Episteme never copies it.

That storage split killed the design that preceded it: a shared `correspondent_records` table keyed by correspondent and period with a JSONB payload. It was invented as the external correspondent's storage, and once external correspondents keep their own database it has no users left. Core stores no content it cannot reason about.

## Transport, and the auth amendment

Push and pull each take a half, along the same episodic/standing line:

- **Episodic content is pushed.** The service files a post when it has one. It knows when it has news; a poll interval would be Episteme guessing, either late or wasteful. And a filed post has to be stored locally regardless, because the feed ranks in SQL (`web/app.py:_rank_expr`) and a post behind an API cannot be ranked, searched or scored.
- **Standing content is pulled.** Glance and the correspondent's page call the service at render time. Never copied, never stale, never migrated.

Push needs an inbound token. **This amends "Single-user forever: no auth" in CLAUDE.md, and the amendment is narrow**: no reader identity, no session, no login, no user or profile columns, no `created_by`. A service token authenticates a machine and never becomes a column on a post. It also does not weaken the rule that the approval card is the only thing between a model and an effect (0036): a service filing a post is the ingest pipeline's kind of effect, and if a chat tool ever gains the ability to file on a correspondent's behalf, that tool is `writes=True` and goes through the card like everything else.

Both credentials are encrypted at rest with a key in `.env`, not hashed. Hashing the inbound token would be stronger against an attacker holding a dump without `.env`, but there is no secrets manager here and no second copy of an issued token, so reading one back in `/admin` is worth more than hardening against an attacker who already reads the outbound credential anyway. The encryption is not theatre: `POST /api/jobs/defer/backup_database` writes `pg_dump` archives to `BACKUP_DIR`, and those files carry the database without the env file.

The gate is structural, not a decorator. One router mounted with an auth dependency, so every route under it is authenticated by construction. A per-route check is a convention that fails silently the first time someone adds a route and forgets, which is the reasoning behind the `writes=True` rule in `llm/chat_tools.py`.

Outbound calls to a correspondent do **not** go through `polite_get`. That throttle and its honest User-Agent exist for strangers' servers and mean nothing when both ends are ours; routing through it would make a page render share a throttle with the ingest crawler. A correspondent client is a direct client, like the host agent's.

## Routes, registration and rendering

Views mount under one core-owned prefix, `/c/<slug>/`, so a correspondent can never shadow a core route. `/c/matsedel` is Matsedel's page and `/c/matsedel/stats` is a view it registers itself. The prefix is deliberately opaque rather than conventional: `/@matsedel` is the one cross-site convention that exists, used by Mastodon, Medium and YouTube, and it means *an account*, which is the one thing a correspondent is not.

Registration copies the split `sources` already uses. A row names the correspondent by slug and carries its configurable aspects, its enabled flag and its two encrypted credentials; the plugin's package has an entry point registering its components, which the registry resolves from the slug the way `get_adapter` resolves `Source.type_name`. `manual.py` is the precedent for a registration whose code does almost nothing, which is how an external service (a row with no plugin) stays legal instead of becoming a landmine in anything that walks all correspondents.

A plugin ships a Jinja template for its Glance block and may ship its own additive stylesheet, scoped to a wrapper core emits and built from the tokens at `style.css:3-40`. Both load on its own pages and on Glance. **Neither ever loads on the feed.** The feed is the one place several correspondents appear side by side, and per-correspondent styling there reads as several applications sharing one page. A filed post is a post and looks like one.

## Glance

The standing-content page is called **Glance** and is the navbar's fourth entry. It is a dashboard: one summary block per correspondent, laid out by core, each block linking through to that correspondent's page. Blocks load as htmx fragments so a slow external service never delays the page, and a block that fails keeps its frame and title and says which correspondent is not responding, with a link that re-fires the fragment. An empty block is indistinguishable from a correspondent that has nothing this week, which is the discipline `llm/host.py:11` already writes down for the status panel.

The name describes the content class, not the layout. "Dashboard" is taken (`admin/admin.html:7`), and naming the page for its current arrangement would make it a lie the first time the arrangement changed.

## Rejected

**A nullable `story_id`.** The cheapest way to file a post with no pipeline behind it, and it costs the property that every post traces back to something. One story per period keeps the column NOT NULL and gives the correspondent an object it controls.

**A second, parallel pipeline for correspondent content.** The thing `manual.py` was written to avoid, for the same reason: one feed, one ranking, one provenance view.

**A shared `correspondent_records` table.** Right until external correspondents kept their own database, at which point it stored data core could not reason about on behalf of nobody.

**Multi-head Alembic now.** Per-plugin `version_locations`, branch labels, a Postgres schema per plugin, and a review gate walking every version location. All plugins currently imaginable ship in this repository, so their tables go in core's single history like any other table. The smell is named rather than filed: a plugin surface whose plugins all ship in-tree is a directory convention. The mechanism gets built when something ships from outside, which the politeness rule makes unlikely for anything that scrapes.

**Names.** "Now" and "Dagens" for the page, both rejected for naming *when* content applies, which collides with future live content (a developing disaster, a launch stream) and with a future daily digest. "Desk" for the correspondent's own word, rejected as a workplace metaphor borrowed from outside the publication. "Listings", rejected because a directory of correspondents has the stronger claim on it. "Almanac" was the recommendation and lost to Glance on the merits.

**The term "Edition"** for a stored period. It answered a question nobody had asked, the design survives its deletion untouched, and "edition" is owed to the daily selection in spec §1. Upserting a corrected period, rather than keeping every capture, removed its last justification.

## Consequences

- `qa_pending()` at `worker/pending.py:60` excludes `aggregate` by name, so every future kind is silently enrolled in main-model review. It becomes an allowlist. `score_pending()` has no kind filter at all and needs one. A filed post touches no pipeline stage at first; scoring is the first to integrate later, QA much later.
- The period key's format belongs to the correspondent and is opaque to core. Re-filing a period upserts, so a menu corrected on Tuesday morning does not become two Tuesdays.
- Archived posts are pruned after `llm_log_retention_days` (30) unless pinned, so a superseded lunch card disappears on that schedule. The correspondent's own tables are where lunch history actually lives.
- Nothing on the external path is on Matsedel's critical path: no ingress, no token table, no outbound client, no degraded block. None of it was built, and nothing is waiting on it - a correspondent that scrapes has to be in-process to be held to `polite_get`, so the first external one will be something that generates its own content.
- Vocabulary is in GLOSSARY.md. The destination and body rules this depends on are 0047.

## What was built, and what was watched

Phase B landed 2026-08-28 as revision `d4b2854cc90c` plus `src/episteme/correspondents/filing.py`. What the design left open and the code had to settle:

- **`models.POST_KINDS` grew from one boolean to a record.** `renders_itself` (0047) is joined by `reviewed` and `scored`, so `qa_pending()` and `score_pending()` read the same table instead of carrying a filter each. That is what "becomes an allowlist" meant: `kind != "aggregate"` enrolled every future kind in main-model review and said so nowhere. `filed` declares all three false.
- **`stories.origin` gains `correspondent`.** The design named the new `status` and forgot the origin beside it, and recording `ingest` would have said the clusterer made a story it never touched, which is the objection that kept `status` off `triaged`.
- **A period has no column.** `source_items.hash` is unique, so re-filing with the same item keys finds the same rows and the story they already belong to is the story for that period. A `period_key` column would have been a second identity for the same thing.
- **A correction updates the post in place** rather than archiving and replacing. A rewrite archives because the old article is worth comparing against; a wrong menu is not, and archiving would change the post id and leave a row the prune deletes 30 days later.
- **Period granularity stays with the correspondent.** `file_post` takes items and a destination and asks nothing about weeks or weekdays, which is what lets Matsedel decide in phase E whether a story is a day or a week.

Watched live in the worker container against the real database, in a transaction rolled back afterwards:

- A filed post comes out `kind=filed`, `sections=[]`, with its href and its `publish_at`; its story is `status=filed`, `origin=correspondent`, `item_count=1`.
- Re-filing the same period returns **the same post id** with the corrected title, and leaves one item and one post.
- The post is in neither `qa_pending()` nor `score_pending()`.
- Setting `sections` on it raises `IntegrityError` from `ck_posts_body_iff_self_rendering`.
- Through `_feed_page` itself: a post scheduled three days out is absent from page 1, and one due a minute ago is present and ranks first.
- The embed server was down throughout, so the degradation was watched too: filing succeeded, logged `No embeddings for filed story 1186`, and left the centroid NULL. That is the documented best-effort path, not a silent failure.

### Phase C: registration and routes

Landed the same day as revision `1e872520d227` plus `correspondents/registry.py`, `correspondents/rows.py` and `web/correspondents.py`. Three things the design left open:

- **The table ships without the two credential columns.** They belong entirely to the external path, which this decision defers with no members, and they cannot be added honestly without an encryption dependency and a key the user has to generate. Two nullable columns are a cheap migration on the day something outside this repository files a post. Written down in the model docstring and the migration's, so the omission is a recorded decision rather than an oversight.
- **A row is created, not seeded.** `seeds.py` only fires into an empty table, so seeding would give a row to a fresh database and never to this one. `rows.ensure_rows` is a get-or-create per registered plugin, run from `bootstrap.py` beside the seeds, and it refreshes a renamed `label` because the plugin owns its own name. A row whose plugin is gone is left alone: its `config` may be the only surviving record of how that correspondent was set up.
- **The stylesheet `<link>` sits in the body.** The app is boosted, so a click swaps `#main-content` and `<head>` is never re-rendered; a link there would arrive on a hard refresh and on nothing else. In the body it comes with the fragment and leaves with it, which is also exactly what "never on the feed" requires. The alternative was `hx-boost="false"` on every link into a correspondent, turning each into a full page load.

The enabled flag is one `Depends` on the include, matching the reasoning the auth gate uses above: routes a plugin adds later are covered by construction. A disabled correspondent answers 404 rather than 403, because there is no reader identity here for whom it could be forbidden.

Watched live: `1e872520d227` applied on top of `d4b2854cc90c` (18.3 MiB pre-migration dump), the table has the six declared columns with `slug` unique, and against the real database a fabricated plugin's row was created once, not created twice, had a renamed label written through, vanished from `enabled_slugs` when disabled, and was deleted afterwards, leaving the table empty. `/c/matsedel` and `/c/matsedel/style.css` both 404 on the running web container, which is what an unregistered slug should do.

### Phase D: Glance

Landed the same day. `web/glance.py` plus `glance.html`, and a `glance` entry in the navbar. What the design left open:

- **Core fetches each block over HTTP rather than rendering it.** The design said a plugin ships a Jinja template for its block, which is true and not sufficient: core does not know what data to render it with. So the plugin owns a route, `GET /c/<slug>/glance`, and the block is an htmx fragment pointed at it. That is the same mechanism an external correspondent's pulled block needs, and it means core never imports a plugin to draw a page.
- **The route is required at registration**, not discovered at render time, where a plugin that forgot it would be indistinguishable from a correspondent that is down. `registry.validate` reads the router's own paths.
- **A page is an include, not a template that inherits.** `correspondent.html` is the leaf and pulls the plugin's template in by name through `web/templating.py:correspondent_page`. Inheritance was the first design and it broke on the first click: `render_block` resolves blocks defined in the leaf only, so a plugin template that extended the wrapper defined neither `content` nor `title`, `fragment_block` answered "whole document", and a boosted click nested the entire page inside `#main-content`. The helper also lives in `templating.py` rather than beside the mount, because a plugin importing back into `web/correspondents.py` is a circular import: that module imports the correspondents package the plugin ships inside. Both were watched crashing, not predicted.

Watched live, with a throwaway plugin installed under the slug `matsedel` and then removed:

- Two blocks side by side, one answering 200 and one 404: the first rendered its list, the second kept its frame and title and said which correspondent was not responding, in red, with a working retry.
- Retry re-fetched **only** its own block. It also exposed two costs that reading the code did not: a `<a href="#">` retry control fired a second request because the whole app is boosted and `preventDefault` does not stop htmx, and the `preload` extension turned on for the whole document in `base.html` preloaded the block 100 ms after the cursor landed on it. The control is a `<button>` now and the block carries `preload="false"`.
- The failure line was showing on **every** block from first paint, because `.glance-block-failed { display: flex }` beats the user agent's `[hidden] { display: none }` on specificity. The markup was right and the test asserting `hidden` passed. Only the browser could find this.
- A boosted click from a block title to `/c/matsedel` carried the in-body `<link>` with the fragment and applied the plugin's CSS (`--accent` on the list rule, read back from `getComputedStyle`), with one header, one sprite and one `#main-content` in the document. Navigating on to the feed dropped the link entirely, which is the "never on the feed" rule holding rather than being asserted.

Not built at the end of phase D: Matsedel itself, and the feed card for a filed post.

### Phase E, first slice: the card, and expiry

Revision `edd1895c150c` adds `posts.expires_at`, and `_feed.html` gains a third branch. What the design left open, and how it was settled:

- **A filed card's byline is read off `sources.type_name`**, which already holds the correspondent's slug. The alternative was a `posts.byline` column, which the user rejected: a second copy of a name that is already in the row. The cost of reading it is that nothing checks the two stay equal, so `filing._require_correspondent_source` makes that an invariant at the one moment the two identities are bound - a source whose `type_name` is not a correspondent slug cannot file. It is checked against the `correspondents` **table**, not the registry, because a row with no plugin is exactly what an external correspondent is.
- **`POST_KINDS` gained `needs_items`.** The feed used to load story items for `kind == "aggregate"`, spelled out at the call site; a filed card reads its items too, and a fourth kind would have been a third place to remember. The registry answers the question now, and `_load_story_items` asks it.
- **The card claims nothing a correspondent cannot back.** No byline, no reading time, no difficulty, no narration: all four are produced by the writer, and nothing wrote this. The kind chip names the correspondent instead, and its date is `publish_at`, because one read of a week mints five posts that share a `generated_at`.
- **The correspondent's label is folded into the feed's ETag.** It is not on the post row, so nothing else in the validator can see a rename, and the stale 304 would leave the old name on screen until something unrelated changed.

Watched live at 2560px, with a throwaway correspondent, source and filed post created in the compose database and then deleted:

- The card rendered at the head of the feed above a real article card: purple `LUNCH PROBE` chip, the source name, `+1 more`, `2026-08-28 21:10` - which is `publish_at` in local time, not the `generated_at` two hours later - and the feedback row. No byline, no minutes, no difficulty.
- `update posts set expires_at = now() - interval '1 minute'` and a reload: the card was gone and the other 20 were untouched.
- The embed server was down throughout, so `_embed_story` took its degraded path and logged `No embeddings for filed story 1189`. The post existed anyway, which is what that path is for.

The 500 that found itself: `feed.html` handed `_feed.html` its context through a `{% with %}` that re-listed the page's keys, and `correspondent_labels` was not in the list. Tests passed because they render the partial with the key supplied. The list is gone - `/` now spreads the same dict `/partials/feed` already passed as its whole context, so both routes hand the template one contract and there is no second place to remember.

### Phase E: Matsedel

Revision `bbe93808d5dc` adds `matsedel_weeks` and `matsedel_dishes`, and `correspondents/matsedel/` is the first real plugin. What the design left open, and how it was settled:

- **One reader for four sites, working on visible lines rather than on each site's markup.** Four bespoke DOM parsers would be more precise and fail the wrong way: a CSS selector that stops matching after a redesign yields an empty menu, and an empty menu is indistinguishable from a kitchen that posted nothing. The line reader has to lose the weekday headings themselves before it stops working, and when it degrades it degrades into a visible line of the site's footer. Two rules carry it, and both are pinned in `tests/test_matsedel.py` against the four pages as they stood on 2026-08-28: a heading is the weekday word alone (optionally with a `24/8` date), and the last block, which nothing bounds, is truncated to the median length of the earlier ones. The strictness is not fussiness - every one of the four pages carries lines like `Mandag-fredag`, `FREDAGAR 150kr` and `Lordagslunch 12-14` that would otherwise open a block and swallow the footer.
- **`ingest_all` excludes any source whose `type_name` is a correspondent slug.** The four restaurants are ordinary `sources` rows, so each keeps its own cooldown, cache validators and a switch in `/admin/sources` for the week one of them redesigns. `enabled=False` was the first idea and it takes that switch away: the flag would then mean two things. The rule is one clause against the `correspondents` table (`worker/tasks.py:not_a_correspondents_source`) rather than a registry lookup, so it also covers a configured correspondent whose plugin is not installed.
- **The plugin's `tasks.py` is not imported by its `__init__.py`.** It imports `worker.app`, and the web process has no business constructing a procrastinate app; the worker imports it by path instead. Everything else the plugin owns is imported at registration, including `models`, because Alembic cannot autogenerate a table whose class was never imported.
- **Stats and summaries share one rule for what a line is.** `Fran koket:` and `BUFFE` introduce the dishes under them rather than naming one, so a card built from them says the same thing every day and a most-served table is four labels. A line counts when it is two or more words and does not end in a colon. It applies to the summary and to `/c/matsedel/stats` only - every line a restaurant wrote is stored and rendered on the week's page.

Watched live at 2560px on 2026-08-28, against the four real sites:

- `POST /api/jobs/defer/matsedel_scrape`, job 87108, 10.037 s: four kitchens read and stored (Kalasboden 9 lines, Koppargrillen 20, Restaurang Italia 43, Restaurang Vänerparken 27, all `2026w35` from `2026-08-24`), then `Filed 5 lunch post(s) for the week of 2026-08-24` as posts 661 to 665, each with four items, `href=/c/matsedel#<date>`, and `publish_at` 06:00 / `expires_at` 14:00 local on its own weekday. That is the whole point of storing the week first: one fetch, five days.
- `/c/matsedel` renders the week as one section per weekday with four kitchen columns, Friday carrying the today accent. It needed the plugin's own stylesheet to widen it: core gives a correspondent page the feed's 58rem measure, which is right for prose and wraps the fourth kitchen onto a row of its own. `.correspondent-page.correspondent--matsedel` is two classes against core's one, and it does not touch the Glance block, which carries the modifier without the page class.
- `/glance` shows the Matsedel block with Friday's menu across all four kitchens, and `/c/matsedel/stats` renders both tables. This is the first block whose route reads the database rather than a fabricated list, and the 8 s `hx-request` timeout still never fired.
- The embed server was down throughout, so all five filings logged `No embeddings for filed story 1190..1194: ConnectError` and the posts exist anyway.
- The exclusion clause was run against the live database after the worker rebuild: 17 sources, 13 kept, the four `matsedel` rows excluded. The worker registered `episteme.matsedel_scheduled_scrape` at `0 2 * * 1`.

The cron is Monday 02:00 **UTC**, which is 03:00 or 04:00 in Vanersborg depending on the season, because procrastinate evaluates crons in the worker's zone. That is hours before any kitchen updates a page and hours before anyone reads a menu, so it was left as a plain UTC cron rather than given machinery to shift it.

Not built, and deliberately: nothing archives a lunch post. They stay published, sink by freshness and are never pruned, which costs roughly 260 rows a year and no work.

Two defects the browser found afterwards, both from the first block that carried a link:

- **`id="2026-08-24"` is legal HTML and `#2026-08-24` is not a legal CSS selector.** htmx resolves a boosted navigation's hash with `querySelector`, so Firefox threw `DOMException: '#2026-08-28' is not a valid selector` and the day the card pointed at was never scrolled to. The anchor is `day-<date>` now, in the week template, the Glance block and `posts.file_week`'s `href`. The five posts already filed were corrected by re-running `file_week` against the stored week, which needed no fetch and left the same five post ids and five story ids in place - the first live observation of the re-file upsert.
- **A Glance block was governing HTML core did not write.** The block body declares `hx-target="this"` so its own fetch swaps into itself, and htmx resolves `hx-target` by walking up from the triggering element, so a boosted `<a>` inside a block found that target before `#main-content` on `<body>`. Clicking the day link swapped the whole `/c/matsedel` page - header, sprite, chat rail - into the block. `hx-disinherit="*"` on the block body is the fix: disinheritance applies to descendants only, so the block's own fetch is unaffected, and every attribute core puts there for its own use stops leaking into the plugin's markup. The rule is general, and the Glance block is the only place in the app that breaks it: nothing else swaps in HTML core did not author. The feed's `load-sentinel` also carries `hx-target="this"` and is safe because it is a leaf that is replaced whole.

Watched after the fix, at 2560px over the tailnet: the day link in the block navigates to `/c/matsedel#day-2026-08-28` with one header, one `#main-content` and one sprite in the document, and a direct load of `/c/matsedel#day-2026-08-26` puts Wednesday 83 px from the top, which is its `scroll-margin-top` under the sticky header. No console errors and no failed requests on either.

### Phase E, after a look at the page

Three changes on 2026-08-29, after reading `/c/matsedel` rather than testing it.

- **A label is rendered as a label.** `Fran Buffe:` was a bullet, which reads as something you can order. The rule that decides is now one function, `readers.is_label`: a trailing colon, or a line with no lowercase letter in it. Both signals are the restaurants' own typography, not an inference about food. **The second half was removed on 2026-08-29; see below.** It had already been written twice - once in `summary_line` as "two or more words and not ending in a colon", once as a `NOT LIKE '%:'` in the stats query - and a third copy for the page is what forced it into one place. The old word-count half was also wrong: `Omelett` is a whole dish and was being skipped as though it introduced something. The stats table now groups in SQL and filters in Python, because the alternative is the same rule in a second dialect that no test can check against the first, and grouping has already collapsed the row count to the small end by then. What the rule misses is on the record: Vänerparken's `Dagens vegetariska` is sentence case with no colon and introduces the line under it anyway; catching it needs a list of phrases, which is a different kind of rule. **That list is `tags.TAGS`, written later the same day; see below.**
- **The restaurants are named once, in a header that sticks under the site header.** The names were repeated in all five day sections. That means `_week_context` hands every day a cell per kitchen in the roster's order, empty where a kitchen published nothing, because the header and each day's row are the same CSS grid and an absent cell shifts every column to its right out from under its name. `--matsedel-columns` carries the roster's length into the stylesheet so both read one number. Below 1100px the bar is hidden, the columns stack, and each cell names its own kitchen again, since a bar cannot name a column that is no longer beside it.
- **Each name is set in the face its own site uses**, hung on the `sources` config key rather than a slugified label, so renaming a restaurant in `/admin` cannot silently drop its typography. Read off the four sites: Koppargrillen sets **Manrope**, Restaurang Italia **Spectral**, Vänerparken **Cardo**, Kalasboden **Open Sans**. All four are Google families, so they are named rather than copied, loaded by an `@import` at the top of `matsedel.css` (a plugin owns no `<head>`, and core injects the `<link>` to that file) with `display=swap` and a real fallback stack behind each.

  **What this is worth is smaller than it sounds, and that is a finding, not a caveat.** Cardo and Spectral are serifs and read as distinct at a glance. Manrope is the WordPress Twenty Twenty-Five default and Open Sans is a template default; side by side they are indistinguishable from each other and from the app's own face. So two of the four say something and two say nothing, because two of these restaurants never chose a typeface. Keeping it costs one external request per page load on an otherwise self-contained app; self-hosting the four woff2 files removes that and needs a static-file route for plugin assets, which core does not have.

Watched at 2560px: the sticky bar holds at the header's height (65px at the time, not the 3.6rem first guessed), the header's grid and every day's grid compute to the same `361px 361px 361px 361px`, all four faces resolve and load, labels render as small caps above the dishes they introduce, and at 900px the bar is gone with each cell naming its own kitchen in its own face.

Two more from the same look, both about the seam under the site header:

- **The header's height is a token, not a literal anyone re-guesses.** The sticky bar sat half a pixel below the header and the week scrolled through the sliver. `4.1rem` was a guess at a measured 65.11px, and `.admin-sidebar` had made the same guess independently at `4.5rem`, 4px out in the other direction. `--header-height: 4.25rem` in `style.css` is now the header's `min-height` as well as what the bar and the sidebar read, so the number is the height by construction rather than a value kept in step by hand. Measured after: header 68px, bar top 68px, gap 0.
- **One frost, not two recipes.** The bar declared its own `88%` background beside the header's `82%` and read as a solid black band under a translucent one. `--frost-bg` and `--frost-blur` are the tokens both surfaces use now; nothing was wrong with the `backdrop-filter`, which was computing to `blur(10px)` the whole time.

**Capitals are not a heading.** The no-lowercase half of `is_label` is gone. It caught four lines across the four pages and three of them were content: Vänerparken writes `BUFFÉ`, then `1`, then the dish, so on Monday `BUFFÉ` heads a list - and on Friday the numbered slots hold `GRILLBUFFÉ` and `DESSERT`, which are what is being served, in capitals, under that same `BUFFÉ`. The same word is a heading one day and content the next, and **no rule reading the line alone can tell those apart**, so the rule must not pretend to. The fourth, Italia's `ITALIENSK BUFFÉ`, reads as an announcement rather than a heading either way. What is left is one signal, the trailing colon, which catches ten lines and gets all ten right.

The cost is on the record: `BUFFÉ` genuinely does head Monday to Thursday, and now renders as a dish. What could tell the two apart is the `1`/`2`/`3` numbering, which says the line after it fills a slot - the reader drops those as empty. Using them means classifying at PARSE time and storing the answer per line, because everything downstream reads `MatsedelDish.text` and nothing else. That is a column on `matsedel_dishes`, so it is a data-model decision and not one the function gets to make; it is the thing to build if `BUFFÉ` as a heading is worth a column.

**What the code cannot read, a human writes down.** Removing the capitals signal left `BUFFÉ` rendering as a dish on the four days it genuinely does head, and it took the card summaries with it: `Restaurang Vänerparken: BUFFÉ` on all five days, and `Restaurang Italia: ITALIENSK BUFFÉ` on the Wednesday. Six of twenty slots, measured. The fix is not a cleverer rule. `correspondents/matsedel/tags.py` is a hand-written table, `site -> exact line -> tag`, and `tag_of` is now the one function every reader calls; `is_label` survives underneath it as the default for a line nobody has looked at. Four tags restore all six summaries and put `BUFFÉ` back as a heading, without the discredited signal returning to the code.

Three tags, and the third is what makes this worth the file. `LABEL` and `DISH` answer the heading question. `HIDDEN` means the line is not part of the menu at all, which is what Kalasboden's `Meny för denna dag saknas.` is - read as a dish it became that day's card headline.

Three properties, each with a test that fails when it is broken:

- **Keyed on the site and the exact line, never on a `matsedel_dishes` row.** A tag is a fact about a phrase a kitchen reuses, so `BUFFÉ` is tagged once and holds for every week, including weeks not yet read. Row keys would die every Monday: `store_week` deletes and rewrites, so the tags would need it rewritten to diff, and even then it is the same 26 lines to re-tag each week. A chore is not a fix. The site is `sources.config["site"]`, the same key the CSS modifier hangs on, so an `/admin` rename cannot silently drop the tags.
- **Applied on read, never at store time.** The table holds what the restaurant wrote, hidden lines included. The tag is presentation policy, and policy that can change must not be baked into stored rows - which is also the only reason the placeholder is fixable at all, since the week it appeared in had already overwritten the real menu. Tagging a line today corrects every week already stored, with no re-scrape.
- **A `HIDDEN` tag cannot name a line one of the four captured pages contains.** It is the one tag that can destroy content, and the fixtures are weeks where all four kitchens published. A hidden tag matching a fixture line is a tag eating a real dish, and the test says so.

Hardcoded rather than a table with an editing UI: four restaurants and one reader, so the tags are versioned, reviewed and branched with the code that reads them, and adding one is an edit rather than a migration, a write route and a form. A fifth correspondent wanting the same thing is the moment to ask whether it should be data.

**A day that comes back with no menu does not replace a day that had one.** Kalasboden took week 35 down mid-week and served `Meny for denna dag saknas.` for all five days. `store_week` deletes and rewrites, so the re-read destroyed the real menu and the five filed posts degraded with it. Recovered from `tests/fixtures/matsedel/kalasboden.txt`, which is the page as it stood on 2026-08-28, and that recovery is only possible because a fixture happened to exist.

The guard is in `store_week`, which is the layer that does the damage. A day whose every incoming line is `HIDDEN`, or that is missing from the read entirely, keeps what is already stored; the kept lines are read back before the delete and rewritten into the same position sequence, so one numbering still covers the week. Per DAY rather than per week, so a site that has published Monday to Thursday and not Friday stores four days and keeps whatever Friday it had.

"No menu" is the same `tag_of` the pages read, which is what ties the two halves together: a placeholder invisible on `/c/matsedel` is also one that cannot overwrite. What the guard cannot notice is a page replacing a real menu with a different real menu, and it must not: that is the site correcting itself, and correcting is meant to win.

**The menu ends where the page starts quoting prices.** Vänerparken's FREDAG block runs to the bottom of the page, and the median cap kept the first footer line under it: `Dagens ratt mandag-torsdag 140kr inkl. smor & brod, maltidsdryck, salladsbuffe samt kaffe & kaka.`, which is what lunch costs and not something anyone can order. `readers.is_terms` is a **boundary, not a filter**, and that distinction is the whole finding: dropping such lines one at a time does not work, because the cap keeps a fixed number of lines, so removing one footer line slides the next one up into it. Watched doing exactly that. Ending the day at the first priced line removes the footer entirely. It ends one DAY and not the page, so a site that prices Monday's lunch still gets a Tuesday. Measured across the four pages: 1 line of 99 matches, and it is the one this exists for. What it would cost is a restaurant that prices each dish on its own line, whose menu would end at the first dish; none of the four does.

The median cap stays, as the fallback for a page that never mentions a price, and it is still load-bearing: without it Kalasboden's Friday reads 32 lines of site footer and Italia's 13.

And the Glance block: **one dish per line, plain, no marker**. Joined with a separator they ran together into a paragraph, and a paragraph has a length, so it was truncated at 140 characters - which meant the kitchen with the most to offer was the one whose menu got cut. A list has no length to trim, and Restaurang Italia's Friday went from five dishes and an ellipsis to all seven. Bullets were the first try and are out: the block is 288px wide, every one of the 14 dishes wraps at that measure, and a column of markers is decoration competing with the kitchen names for the same width. It stays a `<ul>` because it is a list of dishes; only the marker is off. **Labels are rendered as headings here too.** Dropping them was the first try and it loses which dishes are the buffet and which come from the kitchen: `Fran Buffe:` and `Fran koket:` are the only thing on the page saying so, and a flat list of eight quietly asserts they are all the same thing. The kitchen's name does not do that grouping, which is what the block looked like it was arguing.

And the week arrows: **an arrow only where there is a week to go to**, and the glyph alone rather than the glyph plus "previous week". `store.stored_weeks` asks about exactly the two adjacent mondays in one query, and `week_view` hands the template a date or a `None`. An arrow to a week nothing was read for lands on a page saying nothing was read for it, which is a dead end the reader has to back out of. It asks about the adjacent weeks and nothing further on purpose: jumping to the nearest stored week would keep the arrow alive at the cost of what it means, which is "the week next to this one". On a first run both are gone, because the only week stored is the one being looked at. Absent rather than disabled, since there is nothing a disabled arrow tells the reader that its absence does not.

And the name: **Restaurang Vänerparken**, not `Vanerparken`. The seed had it ASCII-folded. `ensure_sources` is get-or-create keyed on `config["site"]` precisely so a corrected name is not overwritten, which also means it does not repair one, so the existing row was updated by hand and the week re-filed from storage - no fetch, the same five post ids and story ids again. The `site` key stays `vanerparken`: it is the CSS modifier and half of every filed item's key, so changing it would orphan the five posts.
