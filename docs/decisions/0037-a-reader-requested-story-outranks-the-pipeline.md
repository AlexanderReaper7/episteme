# 0037. A reader-requested story outranks the pipeline, and says so in a column

- Date: 2026-08-11
- Status: accepted
- Rule: `Story.origin == "user"` means first in the write queue, no `demote_story`, no thin-gate. The interactive lease means the models are not pulled out from under a turn.

## Context

"Make an article from this URL" had no entry point. `SourceItem` requires a `source_id`, and `cluster_items` was the only thing that ever constructed a `Story`, so there was no way into the pipeline that did not begin with a feed.

The tempting shape is a second, parallel path: fetch the page, prompt the model, insert a post. It is also the shape that gets you two writers, two QA stages, two notions of what a post is, and a slow divergence nobody notices until one of them stops being maintained.

The requirement the user added by hand, after reading the plan, is the part that constrains the design: *"and **prioritised**: it run before other work"*. Privilege alone is not enough. The write stage stops at `max_writes_per_run` and at `write_budget_seconds`, so a request that merely survived the ranking could still never reach the model.

## Decision

**Reuse the real pipeline.** `ingest/manual.py:ingest_url` produces exactly the rows the normal path produces: a `SourceItem` under a disabled `manual` source, an embedding, and a `Story` already `status="triaged"`, `triage_decision="write"`. Then it defers `write?story_id=N` and gets out of the way. The same agentic writer writes it, the same QA reviews it, the same feed ranks it.

**A column on the story, not a job parameter.** "The reader asked for this" is durable: it must survive a retry, survive a rewrite that archives the old post, and be visible in `/admin`. It also sidesteps the int-only parameter contract of `defer_args` with no widening.

**Priority is one function, applied at both exits:**

```python
def _user_first(stories: Sequence[Story]) -> list[Story]:
    return sorted(stories, key=lambda story: story.origin != "user")
```

Applied at *both* of `_rank_write_queue`'s returns, so "the reader outranks the pipeline" is one rule rather than a property of one branch. `sorted` is stable, so the ranking it was handed survives inside each group. A test pins the ordering (`[2, 1, 3]` for a requested story sitting between two better-ranked ones), because the rule is invisible in a run where the request happened to rank well anyway.

**Privilege is withheld tools, not refused calls.** `demote_story` is absent from `writer_tools(allow_demote=False)` rather than rejected in dispatch, so the model never spends a step on a choice we were never going to honour. The deterministic thin-gate is skipped too: it exists to spend the nightly budget well, and the reader spending it on a short page is their call.

A consequence worth naming: for a requested story, "no draft" is a **failure**, not an editorial decline, because the tool to decline was not on the table. The log line says so, and the aggregate card is a fallback rather than a verdict.

**The interactive lease.** Nothing knew a chat turn was happening: the only "is anything running" signal is `pipeline_job_running`, a SQL count over procrastinate jobs, and chat runs in the **web** process, invisible to it. So `unload_models()` from the governor, from `pause`, or from `POST /api/llm/unload` would evict the model mid-sentence.

The mechanism is a TTL in `app_state`, not a lock — a web process that dies mid-turn must not strand 20GB of VRAM. One shared rule, `control.unload_unless_interactive`, replaces every *automatic* unload site. `POST /api/llm/unload` deliberately does not route through it: it 409s and offers `?force=true`, the "we did not do it, and here is how to insist" shape `POST /api/llm/backend/stop` already uses.

The governor keeps its two halves apart on purpose. It may still **pause** during a conversation (that is the whole point, the game needs the card); it may not **unload**.

## On politeness (0005, 0006)

The plan flagged this as a possible exception to "all source HTTP goes through `polite_get`", to be written down rather than absorbed silently. On implementation it turned out not to be one: `research.fetch_page` reaches the network through `_pinned_get` → `ingest.http.polite_get`, so a typed URL gets the same throttle and the same honest User-Agent as any source fetch, plus an SSRF guard the source path does not have.

What a typed URL genuinely skips is the **manual robots.txt check performed before adding a feed**. That is a human policy step, applied to a source we then poll forever, and it does not transfer to one page fetched once because the reader named it. Recorded here so the distinction is deliberate.

## Rejected

- **A separate lightweight writer for user requests.** Faster to build, and it forks every editorial rule in the project.
- **A `priority` integer on the story.** A second ranking axis to keep coherent with `rank_score`, to answer a boolean question.
- **A real lock instead of a lease.** Correct until the web process is killed, at which point the models are stranded until someone reads a table.
- **Making the chat turn pause the pipeline.** 0024 lets the governor clear only its own pause, so a chat-authored pause needs an author other than `RESOURCE`. Deferred; contention, not eviction, is the remaining cost.

## Consequences

- A requested story is written even when the page is thin, which is exactly what was asked for and also the way to waste a main-model hour. Watched, not prevented.
- `chat_lease_seconds` is 180 and refreshed per turn. A turn longer than that can still be unloaded under it. The wall clock on a chat turn is 240s, so this is reachable; if it bites, refresh mid-turn rather than raise the TTL.
- Contention remains: a chat turn during a `fast`-model stage forces a swap of roughly 100 seconds each way. The fix is a third resident model on its own port, which is a VRAM budget decision, not a code change.
