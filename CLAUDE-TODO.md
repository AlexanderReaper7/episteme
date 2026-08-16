# CLAUDE-TODO.md

Claude's working list. `TODO.md` is the user's; do not write to it.

What is here: paths that are built and shipping but have not been *watched running*, and quality gaps that are known and not re-measured. The point is to stop me asserting that something works when all I have is that the code exists and the tests pass.

Move an item out when it has been observed live, and say in the commit what was observed.

## Not yet verified live

Watch the next pipeline run rather than assuming these.

- **The QA tool harness and its conditional screenshot** (0026, 0028). A rewrite of a path that *was* working, 134 calls with 81 of 82 posts scored, so the regression risk is real.
- **5 unexplained `400 Bad Request`** responses from llama-server across those 134 QA calls (0026). Cause unknown. Not reproduced since the rewrite, which is not the same as fixed.
- **A writer run producing a multi-question quiz** (0025).
- **Chart, diagram, timeline, glossary and video sections** emitted on a suitable story. The write path was verified 2026-07-19, but not every section type has been seen in output.
- **A pipeline stage running after an agent-driven backend start** (0023).
- **The benchmark sweep and the preset surface** (0041). The host agent was down (503) throughout the 2026-08-15 verification, so `_edit_preset`, `/preset/apply`, `/restart` with `extra_args` and the whole `sweep` scenario have only unit tests behind them. What *was* watched is the degradation: `gate(None)` let the run proceed and `vram_free_mb` stayed NULL. Verifying it means starting the agent, running a two-variant sweep on one model, and then confirming `models-preset.ini` came back with its comments intact and the `; [benchmark]` lines gone.
- **The tray icon as an input device** (0042). Everything else about the console was watched live on 2026-08-15: hidden startup through a real scheduled task (window created at 340 ms, never observed visible), `SW_RESTORE` bringing it up, `SC_CLOSE` deleted, single-spaced logs, the status line, the palette pixel-sampled at `(0,0,0)`. What no test and no probe can do is *click* it. The icon registers (`episteme-llama-agent...SystemTrayIcon`) and sits in the Windows 11 overflow flyout, since `IsPromoted` is unset in `HKCU:\Control Panel\NotifyIconSettings`; drag it onto the taskbar to pin. Left to a human: double-click shows, double-click hides, and each menu item does what it says.
- **`--ok` is now `#17d98e` across the whole web UI** (2026-08-15), changed to match the host agent mark. What was actually looked at is the tray tile and the two SVGs; the green text, borders and chips in `/admin`, `/tune` and the feed have not been seen in a browser since. One thing is known-stale rather than merely unchecked: `style.css` justifies `--neutral-chip-border`'s 65% mix with "grey is darker than `--ok`", which was true of the old olive and is not true of the jade - the two are now near-equal in perceived brightness, so that mix over-compensates. Whether to retune it is a design call, not a bug fix.
- **The installed scheduled task still carries the pre-tray action.** `EpistemeLlamaAgent` is Ready but registered with the old `pwsh.exe ... *> agent.log` command, which redirects stdout and is therefore exactly how the agent decides it has no console to draw in. Re-running `pwsh hostagent/install-task.ps1` is what picks up the `--headless ... -Spawn` action; until then the tray only appears when the agent is started by hand.
- **The code-review fixes of 2026-08-16, in a browser and in the worker.** What was live-verified is only the lease SQL: `hold_interactive` / `release_interactive` / `interactive_held` run against the compose database through the `web` container on a scratch key, both holders, the holder-scoped release, expiry, and the legacy `{"until": …}` upgrade. Everything else is unit tests. Left to watch: the runs partial answering **204** on an idle `/admin/benchmarks` (the `v=` digest, 0033); a non-numeric `fixture_id` returning a 422 page rather than a 500; an unplannable `longctx`/`ladder`/`sweep` reaching status `failed` instead of sitting at `queued` with a cancel button that resolves nothing; and the tray icon disappearing when the console is quit from the palette rather than from the tray. **`control.py` and `bench/runner.py` are worker code**, so none of the lease or runner behaviour is in the running worker until `docker compose build worker`.
- **The two bootstrap defers on `/admin/topics`** (0044). They were retargeted from `#queue` (which they were wiping) to `#defer-propose_topics` / `#defer-apply_topics`. The slot resolves in the live DOM, which is what the old target no longer would, but neither button has been *pressed*: "propose vocabulary" enqueues a real clustering pass over all 829 labels, and "apply proposal" rewrites the topic array on every story and post. Watch the confirmation line land in the slot next time either is run for real.
- **Two legs of the error page** (0043). Watched live 2026-08-16 at 2560px: the 500 on a direct hit, the same 500 swapped in by a boosted sidebar click, and a 404 with its route and path params; `/api` JSON confirmed unchanged over `curl`. Two legs have only unit tests. **The fallback document** (`errors._fallback`), which needs `base.html` or the icon sprite to be the thing that broke - a `test_the_fallback_page_carries_both_tracebacks` unit test is not the same as seeing it in a browser. And **a failing polled fragment leaving the page alone**: the queue partial 500s without `HX-Error-Page`, so htmx should discard it and the reader should keep their scroll, but nobody has watched the queue fail while reading `/admin/jobs`.
- **The `ladder` and `longctx` scenarios** (0039, 0040). Only `quick` has been watched end to end. The ladder exercises `truncate` against a real 19.7k fixture, which is the one place a fixture can be silently mutilated into a different workload.
- **The assistant rail** (0036, 0037, 0038). Watched running 2026-08-12; what is left is listed below, the rest moved out.
  1. The rail survives feed → article → feed with its scrollback and an in-flight stream intact, and `?post_id=` follows the navigation rather than the panel's birth. Not checked in a browser at all yet, only over `curl`.
  2. **A reader-requested story reaching a published post.** The proposal, the approval and the queueing are verified; the write itself has never finished. See the write-budget item below.
  3. `demote_story` **absent** from the tool list in `/post/{id}/provenance` for `origin="user"`. Blocked on 2, since there is no post to inspect.
  4. `?force=true` makes an in-flight turn fail cleanly as an `LLMError` rather than hanging. The 409 and the governor's restraint are verified; the forced-unload-mid-generation leg is not, because no turn would start on a contended GPU.
  5. **A reader-requested story writing through a pause and reaching a post.** The exemption itself is verified (see 0037's consequences: condense ran under a live governor pause where the same job previously returned 0 in 0.068s). What has still never been seen is the main-model pass finishing, for the budget reason below.

## Watched failing, not yet fixed

- **The writer's turn cost outgrows `llm_timeout_seconds` on a dense source.** Story 1031 (a Nature paper, reader-requested) ran four write turns on prompts of 1902 → 5825 → 9598 → 14435 tokens in 49s → 69s → 111s → 153s, then spent the full 600s cap on the fifth and died as `ReadTimeout`, leaving no post. Prompt eval fell from 143 to 15.8 tok/s across the run; at that rate the fifth turn's ~16.6k-token prompt needs ~1000s of prompt processing before a single token is generated, so the cap could not have been met. A game took the GPU partway through (09:29:19, before the fourth turn ended) and made the last turn hopeless, but **the decay from 35 to 1.3 tok/s happened before the game started** and is the real finding: `main` is a 21.7 GB 35B MoE on a 10 GB card, so every turn on a growing prompt is CPU-bound. This is not specific to the assistant; the assistant just routes reader-chosen URLs, which are the worst case, into it.
- **A reader-requested story that fails is retried at the head of the queue forever.** `_user_first` sorts on `origin == "user"` alone and nothing counts attempts, so story 1031 is still `status=triaged, origin=user` and will take the main model first on every subsequent run, ahead of everything the ranker chose. Nothing tells the reader either: they approved a card, were told "queued, first in the write queue", and the failure is visible only in `/admin/jobs`.

## Known content-quality gaps

From the 2026-07-17 run. The machinery was fine; these are the model's output. Targeted by the thin-gate, the writer prompts and the qa stage, but **not re-measured since**.

1. Triage approves `write` for stories with too little source text.
2. The writer restates `summary` sentences verbatim as a prose section.
3. The writer sometimes emits funding/DOI boilerplate as a prose section.

Re-measuring means a capped write run (`?limit=2`) and reading the output, not reading the prompts and concluding they look better.
