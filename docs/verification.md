# Verification log

The agent's working list. `TODO.md` is the user's list, and the agent does not write to it.

This file holds two kinds of entry. The first is paths that are built and shipping but have not been *watched running*. The second is quality gaps that are known and not re-measured. The list exists to stop a claim that something works when the only evidence is that the code exists and the tests pass.

Move an item out when it has been observed live, and say in the commit what was observed.

One exception is kept deliberately separate: a dated section for work a design session **decided** but has not written yet. That is a different claim from the rest of this file, because nothing exists to verify. It gets its own heading and is deleted, not migrated, once built. No such section is open right now.

## Not yet verified live

Watch the next pipeline run rather than assuming these.

- **The web's InferMux key** (0058, built 2026-10-03). The worker's half was watched 2026-10-08, after the deployment gained its key mounts and a route to InferMux (compose `extra_hosts` and the `br-episteme` bridge, nixcfg `modules/nixos/episteme.nix`). During pipeline run 321, InferMux's `/warden/verdict` listed the worker's `/v1/embeddings` in flight as `batch`. When Firefox reached the warden's 25% threshold at 17:27 UTC, the warden yielded, the run ended `paused` with reason `resource`, and a batch call got `gpu_yielded`.

  Not yet seen: an interactive request from the web, which a chat turn makes, going through while the worker's batch calls are refused.

- **The InferMux panel's unload** (0060, built 2026-10-08). The reads were watched the same day against the live InferMux: `/api/llm/backend` listed the loaded embedder with its TTL, `/api/llm/resources` answered in 5 ms, and the dashboard rendered both cards and the link to InferMux's UI. Nobody has pressed unload: its pause is written as MANUAL, which the warden cannot lift, and it was not worth blocking a run to see it.

- **Notifications on their own clock, and the icon on a phone** (0056, shipped 2026-09-10).

  The publishing half is watched. `send_digest` (job 119750) and `matsedel_notify` (job 119751) both reached the real ntfy at `ntfy.example.ts.net`, in 0.144s and 0.047s. Both authenticated as the `episteme` user, which has write-only access to its two topics and nothing else. Anonymous publish answers 403, so the token is doing work. Read back out of ntfy's own `cache.db`, the lunch push carries:

  - the title `Lunch on Thursday 10 September`;
  - four kitchens on four lines;
  - `click` pointing at `https://episteme.example.ts.net/c/matsedel#day-2026-09-10`;
  - `ä å ö` intact with no mojibake, which proves the argument for the JSON format rather than asserting it.

  The digest reported `Episteme: run skipped` at priority 4, which is a real 0055 run row.

  The phone half is partly watched too, over adb (`100.64.0.2:41147`). Both topics are subscribed in the ntfy app against `https://ntfy.example.ts.net`, and both messages arrive. `dumpsys notification --noredact` holds a `NotificationRecord` for `io.heckel.ntfy` on channel `ntfy-high` at importance 4, titled `⚠️ Episteme: run skipped`. The lock screen shows the two grouped under ntfy with `🍽️ Lunch on Thursday 10 September` above it, with the Swedish characters and both emoji intact on the device's own renderer.

  One trap turned up during the check. **While a topic's detail view is open in the app, the app posts no drawer notification at all.** Job 119764 published in 0.050s and produced no record. A check that leaves the app in the foreground therefore looks exactly like a delivery failure.

  What is left:

  - **Neither cron has fired on its own** (06:00 and 09:00 UTC). The two `@app.periodic` registrations have only been seen in the worker's startup log.
  - Four of `compose`'s five branches are unwatched against a real row. Only `status != succeeded` has occurred, and the pipeline has not succeeded since the settings existed, so no digest has ever listed a headline.
  - `matsedel_notify` has never run on a day with **no** post, which is the silent branch.
  - On the device, **nobody has tapped a notification**. The `click` URL is confirmed as far as ntfy's `cache.db`, and the page it names answers 200 with `id="day-2026-09-10"` present. Whether the tap opens it and lands on today's section rather than the top of the week is unobserved.
  - The whole PWA half is untouched for the same reason: Chrome fetching the manifest, the install prompt, and the three icons on a launcher that crops them. Both need the phone unlocked, and adb cannot enter the PIN.

- **A run that loses llama-server halfway** (0055, shipped 2026-09-10). The stage half is live-verified. `summarize_posts`, run against the real database with `LLM_FAST_BASE_URL` pointed at a closed port, raised `LLMUnavailable` after **one** attempt with 36 posts due. On 2026-09-04 the same queue cost 36 attempts and returned 0. The check faked only the pause, which is the loop's first statement and was on (manual, since 06:10 that day). Everything else was real.

  Unwatched: `run_pipeline`'s own branch. Reaching it needs the pre-flight probe to pass and a later call to fail, which means a real llama-server death mid-run. That cannot be arranged without either killing the user's server or faking the probe. A source assertion is the only thing holding it, so the first real outage is the test. Watch for a `pipeline_runs` row with status `skipped` and an error naming the stage.

- **Every page at the window's width** (0051, shipped 2026-08-30). Verified by eye at 2560px on the feed, an article, /admin, /tune and /c/matsedel, and measured at 700px and 390px with no horizontal overflow at any of the three widths. What is unobserved:

  - Nothing between 700px and 2560px has been looked at, so nobody has seen the gap between the `16vw` image and the 40rem breakpoint. A laptop at 1440px or 1920px is the ordinary case, and it is exactly what was skipped.
  - The /admin dashboard's grid makes 5 tracks at 2560px, and the LLM endpoint card sits alone on row one because the card after it spans `1 / -1`. That comes from DOM order, not width, and is left as an open layout question.
  - The /tune sliders are now ~2000px long for a -6..+6 scale. They work, and they look odd.
  - Nothing has been seen at the user's real 150% browser zoom, only at a 2560px CSS viewport. The width is the same, the rendering is not.

- **Every feed card, since `summarize` became their only author** (0050, shipped 2026-08-30). The stage has now run over the whole corpus: 683 posts in 78 minutes. All 688 published posts carry a card, and 80 of them have been scrolled in a browser at 2560px. That settles:

  - both prompts against real input (an aggregate summary reads as a story rather than a rewritten headline);
  - the cost, ~8.8 posts a minute on `fast`;
  - the clamp: 13 of 80 cards clamp, which agrees with the 17% of summaries over 930 characters by `length()`;
  - the expand control opening and closing without navigating.

  What it does NOT settle:

  - No summary has been written by the stage running inside the NIGHTLY pipeline. Every call so far came from a deferred `summarize` job, so the `fast` -> `main` -> `fast` model swap that `run_pipeline` now performs has never happened.
  - `max_summary_source_chars` (24k) has never bound against a real cluster.
  - `markClampedText` re-measuring after a resize, and the collapse's `scrollIntoView`, are both unobserved.
  - Also unwatched by construction: `qa._flush` clearing `summarized_at` during a real review, and a grown cluster re-triggering the stage.

- **The writer reading whole sources** (0050, shipped 2026-08-30). `_condensed_source` is gone and no write has run since. Nothing in the corpus reaches the caps (40k per item, 80k per story), so the branch that trims has never fired against real data. The token cost of the change, a median story of 4.2k chars instead of ~2.5k, is arithmetic, not a measurement of a real prompt.

- **Filing on a clock nobody set by hand** (0046, shipped 2026-08-28). A filed post has been rendered in a browser and watched disappearing when its `expires_at` was moved into the past (0046, phase E). What that leaves:

  - No post has been watched *crossing* either boundary while a page was open. Both boundaries were set by an UPDATE and observed on the next load, not reached by the passage of time.
  - The five-posts-from-one-read case has been produced for real (posts 661 to 665 from one scrape), but no filed post has been watched *appearing* at 06:00 or *leaving* at 14:00 on its own.
  - The embed server was down for every filing so far, including all five Matsedel filings. The *happy* path of `_embed_story` (a centroid actually set, and the filed story reachable from `/api/posts/search`) is the one leg with no observation at all.

- **Matsedel's daily read and its skip** (0054, shipped 2026-09-05).

  The repair itself HAS been watched. `matsedel_scrape`, deferred by hand on 2026-09-05, read all four kitchens live, took week 36 from 2 of 4 to 4 of 4 with 0 placeholder rows, and re-filed the five posts (`Stored Restaurang Vänerparken 2026w36: 27 line(s)`, `Stored Kalasboden 2026w36: 9 line(s)`). That run used the OLD code, so it verifies that the data is fixed and says nothing about the fix.

  Two legs of the fix have since been watched on the REBUILT worker. `matsedel_scrape` on a week that is already whole logged `0 read (-), 4 already whole, 0 not published yet (-), 0 failed` in 0.1s with no outbound request at all. And all four live pages raised `NotPublishedYet` when asked for the week of 2026-09-07, which is the exact case the 2026-08-31 run silently accepted.

  **The week of 2026-09-07 is the first the cron ran on its own, and it held** (checked 2026-09-10). Monday's run at 06:00:50 UTC read for 12.1s and stored week 37 whole: four kitchens, five serve dates each, 9/20/43/27 lines. Italia's 43 lines against week 36's 44 show it is a real, different menu. It filed posts 744 to 748. Tuesday, Wednesday and Thursday each fired on schedule and cost 11 to 16 ms with no outbound request.

  Still unwatched, and each needs a week where something goes wrong:

  - a Monday where some kitchens are behind, and a later morning picks them up while skipping the ones already whole, which is the loop the whole change is for;
  - a filing that happens on Tuesday rather than Monday;
  - the `failed and not read and not whole` condition, which no run has ever met.

- **Matsedel across a week it did not fetch** (0046, shipped 2026-08-28). One scrape has run: four kitchens, `2026w35`, five posts, all three pages rendered at 2560px. What is left needs a second week or a changed page.

  `file_week` re-filing a week has been watched: five post ids and five story ids unchanged, hrefs rewritten. `store_week` has been watched **re-reading** a week it already has, twice. The first time it destroyed Kalasboden's menu (2026-08-29, the defect the guard exists for). The second time, the same day after the fix, it kept it (`kept 5 day(s) the read had no menu for`).

  Still unwatched:

  - a re-read that carries a **different real** menu, which is the branch that is supposed to overwrite (both live runs so far were all-placeholder);
  - one site down while the other three read, which the task is written to step over;
  - a `HIDDEN`-tagged line reaching a page that was already stored before the tag was written, which is the path that makes tags-on-read worth the choice;
  - `week_of` finding nothing, which is what `/c/matsedel` shows before the first Monday of a fresh install.

  The readers have also seen exactly one week of each site. The median cap was measured against four pages on one day, so a kitchen that publishes a short week is unobserved.

- **A correspondent that is not a throwaway** (0046, shipped 2026-08-28). Matsedel answers most of this: `ensure_rows` gave it its row, its block reads the database, and its page and stylesheet load. Two legs are left. One is a block whose route takes **real time**: Matsedel's is two queries, and the 8s `hx-request` timeout has still never fired. The other is a correspondent that ships templates but **no** stylesheet, since Matsedel ships both.

- **The destination rule in a browser** (0047, shipped 2026-08-28). Everything asserted about it over HTTP held. Aggregate cards carry external URLs, `/post/{id}` answers 302 with the right `Location`, `HX-Request` gets a 204 with `HX-Redirect`, article pages still render, and provenance still answers 200. Unwatched:

  - Nobody has *clicked* an aggregate card in a real browser. That is the only way to see whether htmx leaves the cross-origin anchor alone the way its `ut()` same-hostname check says it does.
  - No real triage pass has minted an aggregate. `ensure_aggregate_post` has only run against the live database inside a rolled-back transaction.
  - `_rehome_aggregate_card` has not fired during an actual `cluster` stage. It needs an item to arrive with an earlier publish date than the story's current primary, which was measured at 0 occurrences in 473 posts.

- **The writer and the QA reviewer running on a harness** (0049, shipped 2026-08-29). The assistant's half is watched. A chat turn called `search_posts` through the shared registry and streamed its answer, and `on_tool` fired ahead of the call. The write gate produced a pending proposal (row 17) with **no story created and no job deferred**, and then a rejection that resumed the conversation.

  The writer and QA run in the worker, and the worker's harnesses were only checked structurally: `writer` offers `demote_story`, `writer.requested` does not, and the registry holds 14 tools. **No agentic worker stage has been watched running.** A `resource` pause has held the pipeline since 2026-08-26 and re-contended at 01:00 on 2026-08-29, and `write?limit=1` returned in 0.27s with "1 stories held". Specifically unwatched: `harness.offer` adding `rerender` mid-review during a real QA pass, `Closing` producing a validated `PostDraft` or `QAReview`, and the shared `fetch_page` returning outbound links to the writer's prompt.

- **Any pipeline stage running since the `feature` to `article` rename** (0045). The stored value changed under 173 rows, and every stage's filter changed with it: `narrate` matches `kind == "article"`, `recommend/search.py:71,91` filters on it, and `qa_pending()` and the feed's card branch read it. Verified live on 2026-08-28: the feed renders 6 article and 14 aggregate cards, `/api/posts/search` returns `"kind":"article"`, and the worker booted with its crons. Not verified: a write, a QA pass or a score run producing a post under the new value.

- **The QA editing tools and their conditional screenshot** (0026, 0028). This is a rewrite of a path that *was* working, at 134 calls with 81 of 82 posts scored, so the regression risk is real.

- **5 unexplained `400 Bad Request`** responses from llama-server across those 134 QA calls (0026). Cause unknown. They have not reproduced since the rewrite, which is not the same as fixed.

- **A writer run producing a multi-question quiz** (0025).

- **Chart, diagram, timeline, glossary and video sections** emitted on a suitable story. The write path was verified 2026-07-19, but not every section type has been seen in output.

- **A pause arriving while a stage is mid-story** (0057, 0024). Every pause observed so far found `worker_running: false`, so `unloaded_models` was `[]` every time. The guard that keeps VRAM under a running generation has never had anything to guard. Watching it means starting `write?limit=1` and contending the GPU while it runs.

- **`contended_at` is not drawn anywhere** (0057). The freshness clock is what tells a stranded pause apart from a busy GPU, and `_pipeline_status.html` shows only `since`. Today the distinction exists in `/api/status` and in nothing a person looks at. Showing "last heard from the warden N ago" and marking a pause stale is a design call, not a bug fix.

- **The benchmark sweep and the preset editing** (0041). llama-warden, then `hostagent/` in this repository, was down (503) throughout the 2026-08-15 verification. So `_edit_preset`, `/preset/apply`, `/restart` with `extra_args` and the whole `sweep` scenario have only unit tests behind them. What *was* watched is the degradation: `gate(None)` let the run proceed and `vram_free_mb` stayed NULL. Both of those hosts are gone now, and the warden lives in InferMux (0057), so this entry needs re-planning against InferMux before it can be verified. The old plan was to start the warden, run a two-variant sweep on one model, and then confirm that `models-preset.ini` came back with its comments intact and the `; [benchmark]` lines gone.

- **`--ok` is now `#17d98e` across the whole web UI** (2026-08-15). It changed to match the warden's mark, which has since moved to its own repository (0057) while the colour stayed. Only the tray tile and the two SVGs were actually looked at. The green text, borders and chips in `/admin`, `/tune` and the feed have not been seen in a browser since.

  One thing is known-stale rather than merely unchecked. `style.css` justifies `--neutral-chip-border`'s 65% mix with "grey is darker than `--ok`". That was true of the old olive and is not true of the jade. The two are now near-equal in perceived brightness, so the mix over-compensates. Whether to retune it is a design call, not a bug fix.

- **The code-review fixes of 2026-08-16, in a browser and in the worker.** Only the lease SQL was live-verified. `hold_interactive`, `release_interactive` and `interactive_held` ran against the compose database through the `web` container on a scratch key, covering both holders, the holder-scoped release, expiry, and the legacy `{"until": …}` upgrade. Everything else is unit tests. Left to watch:

  - the runs partial answering **204** on an idle `/admin/benchmarks` (the `v=` digest, 0033);
  - a non-numeric `fixture_id` returning a 422 page rather than a 500;
  - an unplannable `longctx`/`ladder`/`sweep` reaching status `failed` instead of sitting at `queued` with a cancel button that resolves nothing;
  - the tray icon disappearing when the console is quit from the palette rather than from the tray.

  **`control.py` and `bench/runner.py` are worker code**, so none of the lease or runner behaviour reaches the running worker until `docker compose build worker`.

- **The llama.cpp panel in a browser** (2026-08-16). Both dashboard cards now share one 5s poll of `/admin/partials/backend`. The process block replaces itself, and the endpoint card rides back as an `hx-swap-oob` section. Each lifecycle button swaps in a spinner glyph while its request is in flight.

  Verified live over HTTP only:

  - `/admin` renders the poll URL with a real digest and no `hx-swap-oob`;
  - the poll answers **204** on an unchanged digest, and **200** carrying both blocks with the OOB marker on a stale one;
  - the idempotent `start` action returns a `#backend` whose digest is byte-identical to the page's, so the three entry points agree.

  Partly watched since: the profile lock cleared later on 2026-08-16, and `/admin` was loaded at 2560px. Both cards render there, and the endpoint dots read red against a llama-server that was genuinely down.

  The four *dynamic* claims remain unwitnessed: the 5s trigger firing in a browser, the OOB swap landing on the endpoint card, the `htmx-request` glyph swap, and the siblings going inert under `:has()`. Watching the spinner properly means pressing **restart**, which kills whatever llama.cpp is doing, so do it while the pipeline is idle.

  **No live state change has been observed moving the panel.** Every model was `unloaded` and the pipeline held a `resource` pause throughout, so the down-path and the main↔fast swap are only unit-tested against fabricated probe payloads.

- **The endpoint card is only live when a warden is configured.** It rides on the backend panel's poll, and `admin.html` renders that panel only under `status.llm.warden.enabled`. With `LLM_WARDEN_URL` empty the card is a page-load snapshot again. That is no worse than before, but it is not live. The fix is a clock of its own, which costs the guarantee the coupling buys, because two probes can then disagree with each other on screen. A decision, not a bug.

- **The two bootstrap defers on `/admin/topics`** (0044). They were retargeted from `#queue`, which they were wiping, to `#defer-propose_topics` and `#defer-apply_topics`. The new slots resolve in the live DOM, which the old target no longer did, but nobody has *pressed* either button. "propose vocabulary" enqueues a real clustering pass over all 829 labels, and "apply proposal" rewrites the topic array on every story and post. Watch the confirmation line land in the slot the next time either runs for real.

- **Two legs of the error page** (0043). Watched live 2026-08-16 at 2560px: the 500 on a direct hit, the same 500 swapped in by a boosted sidebar click, and a 404 with its route and path params. `/api` JSON was confirmed unchanged over `curl`. Two legs have only unit tests:

  - **The fallback document** (`errors._fallback`). It needs `base.html` or the icon sprite to be the thing that broke. The unit test `test_the_fallback_page_carries_both_tracebacks` is not the same as seeing it in a browser.
  - **A failing polled fragment leaving the page alone.** The queue partial 500s without `HX-Error-Page`, so htmx should discard it and the reader should keep their scroll. Nobody has watched the queue fail while reading `/admin/jobs`.

- **The `ladder` and `longctx` scenarios** (0039, 0040). Only `quick` has been watched end to end. The ladder exercises `truncate` against a real 19.7k fixture, which is the one place where a fixture can be silently cut down into a different workload.

- **The assistant rail** (0036, 0037, 0038). Watched running 2026-08-12. What is left is listed below; the rest moved out.
  1. The rail survives feed → article → feed with its scrollback and an in-flight stream intact, and `?post_id=` follows the navigation rather than the panel's birth. Checked only over `curl` so far, not in a browser.
  2. ~~**A reader-requested story reaching a published post.**~~ Done, and it was already done when this line was written. Story 1031 was written on 2026-08-12 and is published as post 543, with two QA calls behind it (checked 2026-08-29). The `ReadTimeout` below was one run of it, not its end.
  3. ~~`demote_story` **absent** from the tool list for `origin="user"`.~~ This failed: `/post/543/provenance` carries the demote paragraph on a story whose tool list did not have the tool. 0049 fixes it at the root, because one flag now decides the tool list, the prompt slot and the budget-refusal sentence. That was checked structurally in the rebuilt worker on 2026-08-29. Still unwatched **running**: no `writer.requested` run has been observed since, for the pause reason in the harness entry above.
  4. `?force=true` makes an in-flight turn fail cleanly as an `LLMError` rather than hanging. The 409 and the warden's restraint are verified. The forced-unload-mid-generation leg is not, because no turn would start on a contended GPU.
  5. **A reader-requested story writing through a pause and reaching a post.** The exemption itself is verified (see 0037's consequences): condense ran under a live `resource` pause where the same job previously returned 0 in 0.068s. Nobody has yet seen the main-model pass finish, for the budget reason below.

## Watched failing, not yet fixed

- **The writer's turn cost outgrows `llm_timeout_seconds` on a dense source.** A later run of the same story did finish, and post 543 is published. What follows is the failing run.

  Story 1031 (a Nature paper, reader-requested) ran four write turns. The prompts grew 1902 → 5825 → 9598 → 14435 tokens and the turns took 49s → 69s → 111s → 153s. The fifth turn spent the full 600s cap and died as `ReadTimeout`, leaving no post. Prompt eval fell from 143 to 15.8 tok/s across the run. At that rate the fifth turn's ~16.6k-token prompt needs ~1000s of prompt processing before a single token is generated, so the cap could not have been met.

  A game took the GPU partway through (09:29:19, before the fourth turn ended) and made the last turn hopeless. But **the decay from 35 to 1.3 tok/s happened before the game started**, and that is the real finding. `main` is a 21.7 GB 35B MoE on a 10 GB card, so every turn on a growing prompt is CPU-bound. The problem is not specific to the assistant. The assistant just routes reader-chosen URLs, which are the worst case, into it.

- **A reader-requested story that fails is retried at the head of the queue forever.** `_user_first` sorts on `origin == "user"` alone and nothing counts attempts. A failed story therefore takes the main model first on every later run, ahead of everything the ranker chose, and nothing tells the reader. They approved a card and were told "queued, first in the write queue", and the failure is visible only in `/admin/jobs`.

  **Not currently manifesting** (checked 2026-08-29). Story 1031 is `written`, so a later run got through and it is no longer at the head of anything. The mechanism is untouched, so the next reader-requested story that fails does this again.

- **`test_a_log_pane_scrolled_up_is_not_dragged_back_to_the_tail` flakes.** It failed once in three consecutive full-suite runs on 2026-08-28, and passed on its own and on both re-runs. Its docstring already records it failing intermittently before `_append_log` took the last scroll itself, so the immediate-scroll fix narrowed the window rather than closing it. It asserts `is_vertical_scroll_end` with no pause after a 200-line write, which is a claim about Textual's refresh timing, not about the agent. Nothing has touched it since 0042.

## Known content-quality gaps

From the 2026-07-17 run. The machinery was fine; these come from the model's output. The thin-gate, the writer prompts and the qa stage all target them, but nobody has **re-measured them since**.

1. Triage approves `write` for stories with too little source text.
2. The writer restates `summary` sentences verbatim as a prose section.
3. The writer sometimes emits funding/DOI boilerplate as a prose section.

Re-measuring means a capped write run (`?limit=2`) and reading the output, not reading the prompts and concluding they look better.
