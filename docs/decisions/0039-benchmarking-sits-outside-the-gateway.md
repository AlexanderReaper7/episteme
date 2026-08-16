# 0039. Benchmarking sits outside the gateway, and the run row is the parameter record

- Date: 2026-08-15
- Status: accepted
- Rule: `bench/` talks to llama-server directly, never through `llm/gateway.py` (0003). A benchmark job takes one integer.

## Context

0003 says all LLM access goes through the gateway: ask for a role, never a model or a URL. Benchmarking wants the opposite of every service the gateway provides. It wants to name a model explicitly rather than resolve a role; to send a raw message list rather than a schema-validated request; to see the stream's timing chunks rather than a parsed result; to keep a failure as a measured datum rather than degrade past it; and to *not* write an `llm_calls` row, because a benchmark is not work Episteme did on the user's behalf and would pollute the very table fixtures are drawn from.

Routing it through the gateway would have meant adding a bypass flag to every one of those behaviours. The flag set would then be the real interface, and the gateway would be a gateway to two different things.

## Decision

`src/episteme/bench/` is a peer of `llm/`, not a caller of it. `bench/client.py` is roughly a hundred lines of `httpx` against `/v1/chat/completions` and `/v1/models` with `stream=True`, `return_progress=True`, `timings_per_token=True`.

**`LLMError` does not cross into it.** `BenchError` is the module's whole error contract, for the same reason 0003 gives: every caller degrades against exactly one exception type. What differs is what degrading means. The gateway's callers want to continue without the model; a benchmark's caller wants the failure recorded in the row, so `benchmark_sample.error` is a column and a run with two timeouts and four numbers reports both.

**Only streaming exists.** There is no non-streaming path, not as a convenience and not as a fallback, because of two things a benchmark needs that a blocking POST cannot give: the prefill curve (0040), and an abort. Cancelling a `POST` that is not streaming has nothing to notice the disconnect on, so cancellation would be a lie the UI told.

### The run row is the parameter record

`bench_run(run_id: int)` is the whole job signature. Everything the run needs (scenario, models, params JSON, fixture id) is on the row the web handler already created and committed.

The alternative, deferring the parameters as job arguments, puts the definition of a run in two places that can disagree: a procrastinate job payload nobody can query, and a table the page renders from. A run row that exists but has no job is a visible `queued` row someone can cancel or delete. A job that exists with no row cannot happen, because the row is committed first.

It also makes live progress free. The runner overwrites `benchmark_run.progress` (JSONB) as it goes and `web` polls that one row into an SSE stream. The measuring is in the `worker` container and the watching is in `web`; they share Postgres and nothing else, so anything resembling shared memory or a subscription was never available.

### Purity is the test boundary

One real repetition of the `longctx` scenario is about seven minutes on the fast model. So everything that decides *what* gets measured is pure and separate from the thing that measures:

| module | what it is | tested by |
|---|---|---|
| `series.py` | stream chunks → curves and totals | `test_bench_series.py`, on chunk shapes captured from a real b9882 response |
| `report.py` | samples → comparison rows and charts | `test_bench_report.py` |
| `chart.py` | points → SVG geometry | ditto |
| `fixtures.py` | `llm_calls` → a replayable conversation | `test_bench_fixtures.py` |
| `runner.py` | `plan_items`, `gate`, `contaminated` are pure; `execute` is not | `test_bench_planning.py` |

Chart geometry stays on the server for the same reason: a collapsed scale (a perfectly flat line, a single point) is a unit test rather than an unreproducible rendering bug.

## Rejected

- **A `bench` role on the gateway.** The role would resolve to a model, which is the one thing a benchmark must not let happen: comparing two models means naming both.
- **Reusing `llm/observe.py`.** Benchmark traffic in `llm_calls` would corrupt fixture capture, which reads that table for the largest real conversation. The samples are the observability.
- **Job arguments instead of a row.** Two records of one run, one of them unqueryable.
- **A non-streaming fast path for the `quick` scenario.** It would be the only scenario that could not be cancelled or curve-plotted, and therefore the only one whose numbers were not comparable with the others.

## Consequences

- `bench/` duplicates a little of the gateway's HTTP setup. Accepted: the duplication is a hundred lines and the coupling it avoids is every future gateway change having to consider a caller that wants none of it.
- The models a run can name are whatever `/v1/models` reports, so the form degrades to free text when llama-server is unreachable. That is deliberate: a benchmark form has to be usable while the backend is being restarted into the configuration under test.
- `vram_free_mb` and the contention gate come from the host agent (0023) and are `NULL`/permissive when it is absent. Optional infrastructure: no sensor, no opinion.
