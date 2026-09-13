# Plan: benchmarking as an admin page

- Date: 2026-08-15, revised 2026-08-15 after measuring the prefill claim
- Status: **built and live-verified 2026-08-15** for the worker-side scenarios (`quick`), including live progress, cancellation and the charts. The host-agent path (`sweep`, preset editing) is built and unit-tested but not yet watched running; the agent was down during verification, which is itself how the "no sensor, no opinion" degradation got exercised. See [CLAUDE-TODO.md](../../CLAUDE-TODO.md).
- Renamed by [0057](../decisions/0057-the-gpu-decision-leaves-episteme.md), 2026-09-13: the "host agent" throughout this document is now **llama-warden**, its own project at `C:\selfhosting\llama-warden`. The split changed one thing here, the contamination threshold, noted in its own section below. Everything else reads the same with the new name.
- Supersedes: the loose PowerShell in this directory and `C:\selfhosting\llama-cpp\bench-server.ps1`
- Decisions this produced: [0039](../decisions/0039-benchmarking-sits-outside-the-gateway.md), [0040](../decisions/0040-prefill-is-measured-from-the-stream.md), [0041](../decisions/0041-the-host-agent-applies-a-configuration-it-is-handed.md)

Episteme's throughput is currently measured by hand, on the host, in PowerShell, into JSON files nobody reads twice. This plan moves that into the application, because the interesting questions are all ones only Episteme can answer: how fast is the model *on our actual prompts*, does an upgrade cost us anything, and eventually, which of two configurations produces a better article.

## Why now, and what the hand-run measurement already taught us

Three findings from the 2026-08-15 session, each of which the current mechanism surfaced only by accident. They are the requirements.

1. **`ngl = 999` in `models-preset.ini` had silently defeated `fit = on` for months.** An explicit `n_gpu_layers` is a hard constraint the fit solver may not violate, so it aborted, every layer was forced onto the 10 GB card, and WDDM spilled the overflow into system RAM where the GPU read weights back over PCIe. Removing it roughly tripled throughput on both models. Nothing in Episteme would ever have noticed. A benchmark that stores its numbers is how a config regression stops being invisible, **but only if it stores the configuration too**, which is why every sample keeps the model's effective argv (below).

2. **A synthetic 4k prompt overstates prefill by 3-4x.** Qwopus3.6-35B-A3B does 192.5 tok/s of prefill on a 4k prompt and 54.4 tok/s on a real 18.7k writer call. Generation likewise falls from 35.4 to 16.0 tok/s. **Only replayed real prompts predict real wall time**, which is why fixtures come from `llm_calls` and not from a lorem generator.

3. **Warframe launched mid-run and quietly corrupted a benchmark.** Cumulative prefill slid 37.7 to 32.6 to 28.6 tok/s across a single prompt. llama-warden already reports `games_running` from Windows' Game Bar registry and already brakes on contention, so the fix is to reuse both rather than to remember not to play games. **A run that observed foreign GPU load is not a failed run, it is a run whose numbers must be labelled.**

Reference numbers as of 2026-08-15, llama.cpp build 9882 (48719618e), ctx 65536, MTP on, so the first stored run has something to disagree with:

| model | load s | gen tok/s (4k) | prefill tok/s (4k) | prefill tok/s (18.7k) | gen tok/s (18.7k) | MTP accept |
|---|---|---|---|---|---|---|
| Qwopus3.6-35B-A3B-Coder-MTP-Q4_K_M | 10.2 | 35.4 (peak 40.2) | 192.5 | 54.4 / 58.1 | 16.0 / 18.3 | 94.8 % |
| Qwen3.8-27B-MTP-Q4_K_M | 14.7 | 6.1 | 85.0 | not completed | not completed | 84.7 % |

Every figure in that table is a **cumulative average**, which finding 4 says is the wrong number.

4. **The aggregate `prompt_per_second` is not a rate anybody experiences.** Measured 2026-08-15 against the live router, 7697 cold tokens into Qwythos-9B, reading `prompt_progress` off the stream:

   | span | instantaneous tok/s |
   |---|---|
   | 81 → 2129 | 494 |
   | 2129 → 4177 | 165 |
   | 4177 → 6225 | 113 |
   | 6225 → 7697 | 92 |
   | what `timings.prompt_per_second` reported | **152.8** |

   A 5x fall inside one request, and the number the old benchmark stored is one the model stops achieving after the first 2k tokens. Finding 2 was the shallow version of this: it is not that synthetic prompts are unrepresentative, it is that *the aggregate itself* averages away the shape.

## Decisions taken with the user, 2026-08-15

| decision | reason |
|---|---|
| Split by capability: worker executes throughput runs, host agent executes runs needing a server restart | The common case is model vs model on the same server, which is just HTTP and belongs where pause, the governor and job history already are. Flag sweeps (ctx-size, ngl) need llama-server restarted with different arguments, which only the host can do. |
| Results in new Postgres tables, one reviewed migration | Trend, comparison and regression are all SQL over stored rows. Files on disk cannot join against `llm_calls`, which the quality sequel needs. |
| Fixtures are drawn from `llm_calls` | Finding 2 above. A prompt that Episteme did not actually send does not predict Episteme's wall time. |
| The page shows a compare table, run history, live progress, and **within-run curves of tok/s against context position** | The within-run curve is the one thing no existing tool shows, and it is the shape that explains why a 19k call takes 7.5 minutes. |
| The host agent will grow into a model configuration surface | User's stated direction. Compatible with 0023, which forbids the agent holding *policy*, not the agent performing actions. The desired configuration stays in Episteme; the agent applies it, and 0041 records where the line is. |

## Shape

```text
  admin page  /admin/benchmarks
       |
       +-- POST /admin/benchmarks/run  -->  creates benchmark_run  -->  defer bench_run(run_id)
       |                                                                    |
       |    (the run ROW is the parameter record; the job takes one int)    |
       |                                                                    v
       |                                                          worker/bench.py
       |                                                                    |
       |    bench/client.py  --stream-->  llama-server :5001  (measure)     |
       |    llm/warden.py    --HTTP---->  llama-warden :5003   (sense, and  |
       |                                                        for `sweep`,|
       |                                                        apply a     |
       |                                                        preset and  |
       |                                                        restart)    |
       |                                                                    |
       +-- GET /admin/benchmarks/progress/{id}  (SSE, reads benchmark_run.progress)
```

One executor, two capabilities. `benchmark_run.executor` is `worker` for a same-server run and `host` for one that restarted llama-server, and it exists so a flag-sweep row is not silently compared against a same-server row.

**Live progress crosses processes through the database, not through memory.** The measurement runs in the worker; the page is served by web. There is no shared object between them, so the runner writes a small `progress` JSONB onto the run row roughly twice a second and the SSE endpoint polls that row. This is the same trade the log pane makes (0034): the browser is pushed to, and only the cheap internal leg polls.

### The gateway question, and why benchmarking sits outside it

0003 says LLM access goes only through `llm/gateway.py`, and code asks for a *role*, never a model. A benchmark must address a model by name, and must not pollute `llm_calls`.

**Decision: a separate `bench/client.py`, not a `model_override` parameter on the gateway.** The gateway exists to hide the transport from callers who are using the model. A benchmark is not using the model, it is *measuring the transport*, so it legitimately sits outside the abstraction whose whole job is to make the transport invisible. Adding an override to the gateway would weaken the rule for all thirty callers in order to serve one.

Two properties this must have, both of which are the actual reason for the separation:

- **Benchmark traffic writes no `llm_calls` rows.** Otherwise provenance gains rows for posts that do not exist, and worse, fixture selection (which picks prompts by percentile of real traffic) starts selecting benchmark traffic, a feedback loop where benchmarks generate fixtures for benchmarks.
- **It cannot reach a write handler.** It only ever calls completions, never the tool loop, because tools are exactly what the benchmark is trying to exclude.

Recorded as [0039](../decisions/0039-benchmarking-sits-outside-the-gateway.md).

## Data model

Three tables plus two JSONB series. `models.py`, one Alembic revision, reviewed per the gate in CLAUDE.md.

### `benchmark_fixture`

A frozen prompt. **Snapshotted, not referenced**, because `llm_calls` is pruned on a tiered retention (0032) and a fixture that points at a `chain_id` rots the moment its source ages out. Snapshotting is what makes a number from March comparable in September.

| column | type | note |
|---|---|---|
| `id` | int pk | |
| `name` | text unique | `write-p95`, `write-max`, `qa-typical` |
| `kind` | str(16) | `replay` (from history) or `synthetic` (short, for smoke runs) |
| `stage` | str(20) | `write` / `qa` / `triage`, the stage it was captured from |
| `source_chain_id` | str(36) null | provenance only, may dangle after a prune |
| `source_story_id` | int null | same |
| `messages` | JSONB | flattened, tool-free, ending on a user turn |
| `prompt_tokens` | int | advisory only: counted by one model's `/tokenize`, and tokenizers differ |
| `captured_at` | timestamptz | |
| `notes` | text null | |

`prompt_tokens` is **not** the x-axis of anything. Two models tokenize the same messages differently and the chat template adds its own, so every chart uses the per-sample `prompt_n` the server reports for that model.

### `benchmark_run`

One execution of one scenario, covering one or more models.

| column | type | note |
|---|---|---|
| `id` | int pk | |
| `started_at` / `finished_at` | timestamptz | |
| `status` | str(20) | `running` / `succeeded` / `failed` / `cancelled` |
| `executor` | str(10) | `worker` or `host` |
| `scenario` | str(20) | `quick` / `longctx` / `ladder` / `sweep` |
| `fixture_id` | int fk null | null for `quick` |
| `models` | JSONB | requested model names, in order |
| `params` | JSONB | reps, predict, ladder rungs, sweep variants: everything the launch form chose |
| `llama_build` | text | from `GET /props`, so a regression is attributable |
| `env` | JSONB | host-agent `/resources` at start: `vram_free_mb`, `foreign_gpu_percent`, `games_running` |
| `env_end` | JSONB | the same at finish. A run that started clean and ended dirty is the Warframe case, and both samples are needed to say so. |
| `contaminated` | bool | set when the two disagree. Never a comparison baseline, always still visible. |
| `progress` | JSONB | live state, overwritten as the run goes. Not a result. |
| `cancel_requested` | bool | the page sets it, the runner reads it between chunks |
| `error` | text null | |

There is no `ctx_size` column. Context size is per model, not per run, and it is recoverable exactly from the argv stored on each sample.

### `benchmark_sample`

One model, one repetition. `(run_id, model, variant, rep)` unique.

| column | type | note |
|---|---|---|
| `id` | int pk | |
| `run_id` | int fk | |
| `model` | text | the GGUF name the router serves |
| `variant` | str(40) | `""` for a plain run; the sweep's label (`ngl-off`, `ctx-32k`) otherwise |
| `rep` | int | 0 is the discarded warmup, stored anyway because "the first run is 30 % slow" is itself a fact worth seeing |
| `rung` | int null | ladder only: the nominal prompt length asked for. The measured one is `prompt_n`. |
| `prompt_n` | int | prompt tokens actually processed, from `timings` |
| `cache_n` | int | prompt tokens served from cache. **A cold-prefill measurement with `cache_n > 0` is void**, and this column is what proves it was not. |
| `prefill_ms` / `decode_ms` | int | from llama.cpp's `timings` block, **not** derived from wall time |
| `decode_tokens` | int | |
| `wall_ms` | int | includes queueing, so `wall - prefill - decode` is the overhead |
| `load_ms` | int null | model swap cost, first sample of a model only |
| `accept_pct` | float null | MTP draft acceptance (`draft_n_accepted / draft_n`) |
| `vram_free_mb` | int null | sampled **while this model is resident** |
| `args` | JSONB | the model's effective argv, from `/v1/models[].status.args` |
| `prefill_series` | JSONB | `[[prompt_n, tok_s], ...]`, instantaneous, differenced from `prompt_progress` |
| `decode_series` | JSONB | `[[token_index, elapsed_ms], ...]`, bucketed to ~32 tokens |
| `error` | text null | one model failing must not lose the other's numbers |

### `args`, and why it is the most valuable column here

Finding 1 is the whole motivation and the numbers alone cannot serve it: throughput says *that* something regressed, never *what changed*. The router hands the answer over already, in `/v1/models`:

```json
{"id": "Gemma4-12B-…", "status": {"value": "unloaded", "args":
  ["…llama-server.exe", "--ctx-size", "65536", "--flash-attn", "on",
   "--fit-target", "768", "--model", "C:\\selfhosting\\models\\…"]}}
```

`n_gpu_layers` present in the argv of a run that got 16 tok/s and absent from the argv of the run that got 35 is the regression diagnosing itself, in stored data, with no human remembering to note it. Per sample, not per run, because flags are per model.

### The two series, and why they are JSONB and not a fourth table

`decode_series` is `[[token_index, elapsed_ms], ...]`, bucketed to ~32 tokens because per-token timings on a 16 tok/s model are dominated by chunk-boundary jitter. A 2048-token generation is 64 pairs.

`prefill_series` is `[[prompt_n, tok_s], ...]`, one point per `prompt_progress` chunk, differenced (below). llama-server reports progress per `n_batch`, so a 19k prompt yields about ten points.

Both are columns rather than a `benchmark_tick` table because they are **never queried row-wise**. Nothing will ever ask "which ticks exceeded 40 ms"; the only consumer is a chart that reads the entire series at once. A table would buy indexing nobody uses and cost thousands of rows per sample. This is the same reasoning that put `pipeline_runs.stages` in JSONB.

### The seam for quality comparison

The stated sequel is comparing prompts, toolchains and models on the *quality* of the finished article, not its speed. That is a different measurement over the same axes, so it gets `benchmark_judgement (sample_id, dimension, score, rationale, judge_model)` later, and nothing above needs to change to accommodate it. **Do not build it now.** It is recorded here only so the fixture and run tables are not shaped in a way that forbids it: specifically, `benchmark_fixture.messages` keeps the full research text rather than a token count, which is what a quality run would need to replay.

## Measuring tok/s against context position

Both halves come off **one streamed request**, which is the reason this is one phase and not three.

```jsonc
{"model": …, "messages": …, "stream": true,
 "return_progress": true,      // prefill curve
 "timings_per_token": true,    // timings in the final chunk
 "cache_prompt": false}        // cold, and `cache_n` proves it
```

### Prefill: differenced progress, measured not inferred

`return_progress` makes llama-server emit, during prefill and before any token exists:

```text
{'total': 7697, 'cache': 0, 'processed': 2129, 'time_ms': 4268}
{'total': 7697, 'cache': 0, 'processed': 4177, 'time_ms': 16708}
```

The counters are cumulative, so the instantaneous rate is `Δprocessed / Δtime_ms`, and that differencing is the entire trick. Plotting the cumulative figures directly would reproduce the exact error the old benchmark made: **a cumulative average understates how far the instantaneous rate has actually fallen** (152.8 reported against 92 real, above).

The previous revision of this plan said "there is no honest way to see inside prefill over HTTP" and proposed scraping the llama-server log (0034) for its per-chunk lines. That was wrong, measured wrong on build b9882, and the log path is dropped entirely. Never parse a log for a number the API returns. Recorded as [0040](../decisions/0040-prefill-is-measured-from-the-stream.md).

### Decode: stream and timestamp, and get live progress for free

Record `(token_index, elapsed_ms)` as each content chunk arrives and the decode curve falls out. Decode slows as the sequence grows because each new token attends over everything before it, so this curve is the direct picture of that cost.

**The same stream drives the live progress pane**, and now it drives it during prefill too, which is where most of the wall time goes. A pane that showed nothing until the first token would show nothing for six minutes of a 19k writer call.

### The ladder: still built, demoted to a cross-check

Truncate one fixture to 2k / 4k / 8k / 16k / 32k tokens and run each as its own sample, `cache_prompt: false`. This measures something the within-request curve does not: total cold-prefill throughput as a function of total prompt length, which is what predicts wall time for a prompt of size N. Truncation is on message boundaries, and the rung is a label; `prompt_n` is the x-axis.

The two are **not plotted as one series**. Ladder points are aggregates over a whole prompt, curve points are instantaneous.

### Streaming does not cost the aggregate numbers

Verified 2026-08-15: the final SSE chunk of a streamed request carries the same `timings` block as a non-streamed one, `draft_n`/`draft_n_accepted` included. So there is no non-streaming executor at any point, which matters for a reason beyond tidiness (next section).

### Other parameters sampled during a run

Polled from llama-warden's `/resources`, stored on the sample and in `env`/`env_end`:

- `vram_free_mb` (does a long context grow the KV/state buffers mid-run?)
- `foreign_gpu_percent` and `games_running` (did the measurement get contaminated, and when?)
- `our_gpu_percent` (a CPU-bound model shows low GPU utilization, which is the visible signature of the 27B's 6 tok/s)

`/resources` costs ~3.5 s per call (0023, measured), so it is sampled at the boundaries of a sample and never on the request path.

## Cancellation, which is why there is no non-streaming path

The 2026-08-15 incident had an asymmetry that made it expensive: the client was killed but llama-server kept prefilling with no client attached, holding the GPU at 100 % for another ten minutes. **Cancelling a benchmark must abort the request server-side, not merely stop reading the response.**

Dropping the connection is the only abort llama-server offers, and it only helps while the server is streaming to you: a non-streaming request has nothing to notice the drop on until it tries to write its one response. So a non-streaming executor cannot satisfy this, which is the second reason streaming is not a later phase.

`cancel_requested` on the run row is the button; the runner checks it between chunks, closes the stream, and stores the run as `cancelled`.

## Contamination, handled rather than remembered

Before starting, read `/resources`. If `games_running` is non-empty or `foreign_gpu_percent` is at or above **the warden's** threshold, **refuse and say what is running**. That threshold is read off `/verdict` at the run boundary rather than configured here (0057): two numbers meaning "the GPU is busy" in two projects would drift. Note the asymmetry on games, which is deliberate: the warden decides on load alone, because a minimised game holds VRAM without using the card, while a benchmark taken beside a running game is meaningless whatever the load says. After finishing, read it again and stamp `env_end`. If the two disagree, the run is stored with `contaminated = true`, stays visible in history with a label, and is never used as a comparison baseline.

The warden and the benchmark also have to agree about who owns the GPU: a benchmark holds the interactive lease (`worker/control.py`) so nothing unloads the model underneath it, refreshed on a timer because `chat_lease_seconds` is 180 and a longctx run is 7 to 24 minutes.

## The page

`/admin/benchmarks`, one more entry in `admin/_sidebar.html`, a new `admin_benchmarks.html` extending `admin_base.html`. htmx and Jinja, no charting library; the curves are plain inline SVG, which is what a two-series line chart deserves.

Sections, top to bottom:

1. **Launch.** Scenario, fixture, models (multi-select from the router's `/v1/models` via `gateway.list_models`, which is not a completion call and so does not touch 0003), reps, predict. Posts to `/admin/benchmarks/run`, its own route rather than `/admin/defer/…`, because the generic defer path validates against four int parameters and a model list is neither.
2. **Running.** Present only while a run is active. Live progress over SSE, prefill and decode. Carries `hx-target="this"`, per defect 2 in 0023, which cost a dashboard once already.
3. **Compare.** The run's models side by side: load, prefill tok/s, decode tok/s, accept %, wall, free VRAM. Deltas against the previous uncontaminated run of the same scenario, so "3 % slower" is visible without arithmetic.
4. **Curves.** Decode tok/s against token index and prefill tok/s against position, one line per model, plus the ladder chart when the run has ladder samples.
5. **History.** Past runs, newest first, tagged with `llama_build` and flagged when contaminated. **A contaminated run stays visible and labelled rather than being hidden**, because the label is the lesson.

## Phases

**A. Schema, executor, page.** `models.py`, one reviewed migration, `bench/` (`client.py`, `series.py`, `fixtures.py`, `runner.py`), the `bench_run` task, the page with launch, live progress, compare, curves and history. Streaming from the first line, per the two arguments above. Verifiable end to end: run `quick` on both models and reproduce the table at the top of this document, with the shape the table cannot show.

**B. The ladder.** Fixture truncation and the `ladder` scenario. Small, because the executor already produces every column it needs.

**C. The host executor.** Host agent gains a preset read/write surface and a start that takes extra arguments; Episteme gains the `sweep` scenario, which applies a variant, restarts, measures, and restores the original preset unconditionally. Lands with [0041](../decisions/0041-the-host-agent-applies-a-configuration-it-is-handed.md).

**D. Not now: quality judgement.** Recorded above as a seam only.

## Open questions, resolved

- **Retention.** Never pruned. Benchmark rows are small (the only size driver is the two series, ~1 KB a sample) and their entire value is in being old. No `pinned` column, and `prune_job_history` does not touch these tables.
- **Does a benchmark respect the pipeline pause?** It brakes on *foreign* contention only, never on the pipeline's own pressure, and it takes the interactive lease so nothing unloads the model underneath it. Somebody benchmarking is deliberately using the GPU, which is the one case where an automatic pause is wrong.
- **The two existing PowerShell scripts.** `bench-matched.ps1` and `bench-load.ps1` (2026-08-01) measure concurrency and load, which this does not cover. Kept until something replaces them. `bench-server.ps1` is superseded and can go once a run has been watched end to end.
- **Where this document is cited from.** [docs/decisions/README.md](../decisions/README.md) already lists `../benchmarks/` under Investigations; it now has the one-line description every other entry has.
