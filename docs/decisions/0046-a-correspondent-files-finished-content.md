# 0046. A correspondent files finished content, and owns a page for what stays true

- Date: 2026-08-28
- Status: accepted, not yet built
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
- Nothing on the external path is on Matsedel's critical path: no ingress, no token table, no outbound client, no degraded block.
- Vocabulary is in GLOSSARY.md. The destination and body rules this depends on are 0047.
