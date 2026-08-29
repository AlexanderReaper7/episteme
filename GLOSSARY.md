# Glossary

When a name or meaning changes, this file must also change in the same commit.

## Feed

### Post

Any single item in the feed, of whatever kind. All feed content is a post (0011), so every visible unit has one id from triage verdict to archival.

### Kind

A post's skeleton: `article`, `aggregate` or `filed`. The spec reserves `micro` and `game` (§12). The set is `models.POST_KINDS`, which records for each kind whether it renders itself (0047), whether QA reviews it and whether the profile scores it; `Post.kind` refuses a value that is not in it, so a new kind cannot exist without answering all three.

### Article

The long-form written kind, what the writer produces. Title, summary and typed sections are filled. Previously called `feature` until 2026-08-28 (0045).

### Aggregate

An identity-only row for a cluster card. It stores no content beyond its href; the card renders from its story's items at read time and links straight to the primary item.

### Section

One typed member of the nine-member union in `llm/schemas.py`, the unit an article's body is built from (0013). Media is a closed set drawn from ingested items, never a URL the model invented.

### Href

Where a post's card and its canonical URL both go (0047). NULL means the post renders itself at `/post/{id}`; an aggregate's is its primary item's URL, a filed post's is its correspondent's page. A post carries a body exactly when its href is NULL, and the database enforces it.

### Primary item

The item that stands for a story: earliest `published_at`, then lowest `id` (0047). It supplies an aggregate card's title, source name, snippet and destination, which is why it is one definition rather than four call sites.

### Story

A cluster of source items about the same underlying event or paper. Every post hangs off one; `Post.story_id` is NOT NULL.

### Publish at

When a post becomes visible (0046). NULL means immediately. It lets a correspondent create a whole week of posts from one read and have each appear on its own day, without depending on a nightly job to mint it. The feed filters on it and sorts by it.

### Origin

Who asked for a story: `ingest` is the pipeline's own clustering, `user` is a request through the assistant, which outranks the pipeline (0037), `correspondent` is content that was filed already finished (0046).

### Filed

A story's fourth and terminal status, for content a correspondent handed over finished (0046). It never passes through `new`, so `triage_pending()` and `write_pending()` miss it by construction: both match a status by name rather than excluding one.

### Archived

A post that left `published`, superseded by a rewrite or demoted by QA. At most one published post per story at any time.

## Ingestion

### Source

One row naming an adapter plus its config. It carries a credibility rating, HTTP cache validators and a cooldown.

### Adapter

The per-source-type implementation of `fetch` and `extract`, registered by `type_name` (0002).

### SourceItem

One fetched thing, normalized and deduped by `sha256` of its canonical URL.

### Polite and impersonate

The two HTTP transport modes (0006). Both obey the same throttle; only the fingerprint differs, and neither may reach a path robots.txt disallows.

## Pipeline

### Stage

One named step: `embed`, `cluster`, `triage`, `write`, `qa`, `score`, `narrate`. Each picks up whatever rows are unprocessed, so stages run independently.

### Triage

The `fast` model's verdict on a cluster: `write`, `aggregate` or `skip`.

### Attempt

One run of the writer over one story, identified by `attempt_id` on its LLM calls, so a failed attempt keeps its own provenance (0012).

### Provenance

History of how the post was made. Every LLM call behind one post version, rendered as the conversation it was.

## Models

### Role

What callers ask for, never a model name or a URL (0003): `main` is the large model that researches, writes and reviews; `fast` is the cheap first pass; `embed` is the embedding model; `chat` is the assistant's (0038).

### LLM Gateway

`llm/gateway.py`, the single choke point every call passes through. `LLMError` is its whole error contract.

### Router

The llama-server on port 5001, holding at most one decode model.

### Embed server

The llama-server on port 5002, a separate process from the router and always resident.

### Host agent

The process on the host that starts, stops and senses llama-server. It is a sensor and an actuator, never a decision maker (0023).

### Harness

Everything one agentic run needs, in one object built per run: which model, what it is told, what tools it may call, how long it gets, and how the run ends (0049). The writer, the QA reviewer and the assistant differ only in their harness. A condition known before the run starts is a filled slot in the prompt; one that arrives mid-run is `offer`, which adds a tool and returns the sentence that tells the model about it.

### Tool

One entry in the registry every harness selects from, `llm/tools.py`. It carries the schema the model is shown, the handler, the least context that handler needs, whether it is terminal, and whether it writes.

### Tool context

A harness's per-run state, handed to every handler. Budgets live here rather than in `settings`, which is what lets one `web_search` serve a writer allowed six searches and a chat turn allowed two.

## Recommendation

### Feedback

One recorded event. The feedback log is canonical and everything else is derived from it by replay (0017).

### Profile

The derived state: topic weights, liked and disliked centroids, source weights, hard blocks.

### Topic

A row in the canonical vocabulary, carrying a display label that may be renamed at will.

### Slug

A topic's permanent identity, and what weights are keyed by. `slugify(label)` stops finding a topic the moment someone renames it (0019).

### Affinity

A post's stored match against the profile, deliberately without the freshness term, which the feed query applies instead (0020).

### Block

A hard exclusion, defined once in `recommend/blocks.py`. A reader-requested story is exempt.

## Correspondents

### Episodic content

Content that is read once and then done with. The feed is entirely episodic: a post is news for a day, and the promise that a reader can be caught up depends on there being a finite amount of it.

### Standing content

Content that stays true until it changes, looked up rather than caught up on. This week's lunch menu is standing; today's menu is episodic. Standing content has its own page and never enters the feed.

### Correspondent

Something that produces finished content on its own schedule and files it, skipping triage and the writer (0046). It files episodic posts into the feed and owns a page under `/c/<slug>/` for its standing content. Episteme gives it identity, storage and its place in the app; how it gets its content is entirely its own business. A **plugin** correspondent ships inside this repository and runs in-process; an **external** one is a separate application that keeps its own database, pushes posts over an authenticated endpoint, and serves its standing content on request. The row in `correspondents` is the configuration and the plugin the registry resolves from its slug is the code; neither implies the other, which is what lets an external correspondent be a row with no plugin. Core owns the `/c/<slug>/` prefix and mounts the plugin's router under it, with the enabled flag as a dependency on the mount.

### Filing

Both the act and the entry point, `correspondents/filing.py:file_post` (built 2026-08-28): a correspondent files a finished post, instead of handing the pipeline raw material to generate one from. Re-filing a period upserts, and the story is found through its items' hashes rather than through a `period_key` column that does not exist.

### Period key

The identity a correspondent gives one period of its content. Opaque to core, chosen by the correspondent, and unique within it: Matsedel's is `Matsedel/koppargrillen/2026-08-24`, one restaurant's lines for one day, whose `sha256` becomes the `SourceItem.hash`. Re-filing a period upserts, so a menu corrected on Tuesday morning does not become two Tuesdays.

### Glance

The page for standing content, and the navbar's fourth entry (0046). A dashboard of one summary block per correspondent, each block linking through to that correspondent's own page. Core lays the blocks out and fetches each from the correspondent's own `GET /c/<slug>/glance`, so a slow one delays only itself and a block that fails says which correspondent is not responding rather than rendering empty. The feed never carries standing content.

### Matsedel

The first correspondent, built 2026-08-28 (`correspondents/matsedel/`, 0046): the lunch menus of four restaurants in Vanersborg, read from their own sites once a week. A plugin rather than an external service, because it fetches from someone else's site and only in-process code can be held to `polite_get`. One read of a week is stored and then files five posts, one per weekday, each publishing at 06:00 and expiring at 14:00 local. The week itself lives on `/c/matsedel` and outlives the posts.

### Line

What Matsedel stores. A restaurant writes lines, not dishes: `Fran koket:` introduces the ones under it, `1` numbers a buffet, `VECKANS VEGAN: ...` is the week's standing option. Every line the restaurant wrote is stored, and nothing decides what a line IS until something reads it back. **A label** is a line that introduces the lines under it, a **dish** is something you can order, and a **hidden** line is not part of the menu at all (a placeholder a site prints for a day it has not published). `tags.tag_of` is the single function that decides, and every reader goes through it: the week page and the Glance block render a label as a heading and drop a hidden line, a feed card's summary quotes the first dish, the most-served table counts only dishes. It answers from `tags.TAGS`, a hand-written table keyed on the restaurant's site and the exact line, and falls back to `readers.is_label` (a trailing colon, the restaurant's own punctuation) for a line nobody has tagged. **A tag is an override on that default, not a replacement for it**, so a restaurant added next year renders on its first week. Tags are applied on read: `matsedel_dishes` holds what the restaurant wrote, hidden lines included, so tagging a line today fixes every week already stored. `store_week` reads the same tag for a second purpose: a day whose every incoming line is hidden, or that a re-read dropped, keeps what is already stored rather than being overwritten with nothing.

## Proposed, TBD, WIP

Anything that is Work-In-Progress, proposals, etc, goes here.

