# 0037. A reader-requested story outranks the pipeline, and says so in a column

- Date: 2026-08-11
- Status: accepted
- Rule: `Story.origin == "user"` means written immediately and through a pause, first in the write queue, no `demote_story`, no thin-gate. The interactive lease means the models are not pulled out from under a turn.

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

## A pause does not hold a requested story (added 2026-08-12)

Priority was originally ordering only, and that turned out not to be priority at all. Approval already defers `write?story_id=N` immediately, so the story never waited for the 03:00 cron; what it waited for was the **pause flag**, because `write_posts` returned 0 at its first check. Watched live: the approval deferred job 43858, which succeeded in 0.068 seconds having written nothing, and the reader was told "queued". "Start it now" meant "maybe tonight".

So the rule became explicit about what a pause is *for*:

```python
def _pause_stops(story: Story) -> bool:
    """A pause brakes work the machine chose to do. A story the reader asked for
    is not that."""
    return story.origin != "user"
```

Applied at both of the write stage's pause checks, the same shape as `_user_first` at both of `_rank_write_queue`'s exits. Pass 1 `continue`s rather than `break`s, so the exemption is a property of the story and not of its position in the queue, and pass 2 iterates `prepared` rather than `stories`, which makes "written without a seed" a `KeyError` that cannot be reached rather than one guarded by hand.

This overrides a **deliberate** pause too, not only the governor's. Asking for an article is the newer instruction, and the circularity is real: the governor reads the chat turn's own GPU load as contention, so the request routinely provokes the pause that would have held it. The cost is that "pause everything, I am gaming" no longer means everything; the writer's tool result says so in the same breath, and turning it back into a resume-only wait is one predicate.

The test is an **AST check** rather than a behavioural one: exercising `write_posts` needs a database and a model, and the failure mode is a third pause check added later without the guard. It reads `write_posts`'s source and asserts every `pause_requested` call sits in an `and` with `_pause_stops`. Verified to fail for that reason by removing one guard (`assert 1 == 2`). `tests/test_icons.py` reads source for the same reason.

## The lease is a set of named holders (added 2026-08-16)

The lease was one expiry under one key, which was right while chat was the only thing that could hold it. Benchmarks (0039) made it a second deliberate GPU user, on a wildly different clock: a chat turn is minutes and a `longctx` run is hours, and they overlap freely because the reader starts a benchmark and then asks the assistant about it.

With one shared expiry, `release_interactive` at the end of a chat turn handed the card back **on the benchmark's behalf**. The runner refreshes every `bench_lease_seconds / 3` (100 s), so the window between the chat turn ending and the next refresh is up to a hundred seconds in which the governor may unload the model — in the middle of a measurement whose whole purpose is that nothing moved underneath it. Symmetric in the other direction: a benchmark finishing mid-conversation released the chat's lease.

So the value is `{"holders": {name: expiry}}`, `interactive_held` is true if any holder is unexpired, and a release names exactly one. `CHAT_HOLDER` and `BENCH_HOLDER` are constants rather than literals, so a hold and its release cannot drift apart by a typo — which would leak a holder until its TTL, with nothing to say why the card was busy. Expired entries are left in place: the set of names is bounded and small, and `interactive_held` already ignores them, so a sweeper would be a second thing to keep correct.

**Both writes are one SQL statement, not read-modify-write.** The holders live in different *processes* — chat in `web`, the benchmark in `worker` — so a Python-side merge under READ COMMITTED loses whichever transaction commits second. That is the same bug again, just rarer and harder to see. `INSERT … ON CONFLICT DO UPDATE` with a `jsonb` merge for the hold, `- :holder` for the release.

Raw SQL has its own trap and it was walked into on the way: `text()` parses `:name` with a negative lookahead on `:`, so a postfix cast swallows the parameter. `to_jsonb(:until::text)` binds `unti`, and nothing says so until the statement executes. The entry is now built in Python and passed as one `cast(:entry as jsonb)` parameter, and `tests/test_governor.py` asserts mechanically that every statement's parsed bind names equal the parameters handed to it.

Verified live against the compose database on 2026-08-16, with the real functions: both holders held, chat released leaving the benchmark held, benchmark released leaving nothing, an expired holder reading as not held, and a legacy `{"until": …}` row still honoured and then rewritten into the holders form by the next hold.

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
- **`POST /api/pipeline/pause` no longer stops a reader-requested write**, by construction. The remaining brake is `POST /api/llm/unload?force=true`, which makes the writer fail as an `LLMError`. Anything gentler needs the pause to carry an author the exemption respects, which is the same authored-pause machinery 0024 already describes and this deliberately did not build yet.
- The exemption was watched working 2026-08-12: with `pipeline_pause.paused = true` (governor, `resource`, a game holding the GPU), `write?story_id=1031` ran condense through the fast model (55s, 1271 prompt tokens) and entered the main-model pass. The same job before the change returned 0 in 0.068s.
- `chat_lease_seconds` is 180 and refreshed per turn. A turn longer than that can still be unloaded under it. The wall clock on a chat turn is 240s, so this is reachable; if it bites, refresh mid-turn rather than raise the TTL.
- Contention remains: a chat turn during a `fast`-model stage forces a swap of roughly 100 seconds each way. The fix is a third resident model on its own port, which is a VRAM budget decision, not a code change.
