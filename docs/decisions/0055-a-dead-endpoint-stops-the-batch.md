# 0055. A dead endpoint stops the batch, and the run says so

- Date: 2026-09-10
- Status: built, unit-tested; not yet watched against a real llama-server death (see [verification.md](../verification.md))
- Rule: `LLMUnavailable` is an `LLMError` raised when the connection was never established. No stage loop may swallow it. A pipeline run that hits one stops there and is recorded `skipped`, never `succeeded`.

## Context

Pipeline run 208, 2026-09-04, was recorded `succeeded`:

```json
{"embed": 32, "cluster": 32, "triage": 28, "write": 6, "qa": 2, "summarize": 0, "narrate": 0, "score": 725}
```

`"summarize": 0` reads as a stage with nothing to do. What actually happened is in `llm_calls`:

| stage | calls | errors | window |
|---|---|---|---|
| qa | 18 | 8 | 09:26:31 to 09:54:27 |
| summarize | 36 | 36 | 09:54:27.406 to 09:54:28.324 |

llama-server dropped at 09:54:27, mid-QA, with one `Server disconnected without sending a response`. Every call after it returned `All connection attempts failed` in 6 to 43 ms. QA burned its six remaining posts, `summarize` put all 36 pending posts through a closed port in 0.9 seconds, and the orchestrator wrote `succeeded`.

The same thing had already happened on 2026-08-31: 11 summarize calls, 11 failures, run 203 `succeeded`.

Cost: 32 published posts had no summary, and `_visible_now` hides a post without one (0050), so they were not in the feed. Six days, 26 aggregates and 6 articles, no error anywhere in `/admin`. The nightly cron kept firing and kept skipping on the pre-flight probe, which is a different and correctly-reported condition, so the job history looked like an ordinary outage rather than a run that had thrown work away.

## Why every loop swallowed it

Each stage catches a failed call per item and moves to the next one. That is deliberate and right: one story whose triage output will not validate must not cost the other 27, and the nightly stage picks it up again tomorrow because selection is data-driven.

The sentence that handler is saying is "skip this item". When nothing is listening on the port, that sentence is false, and a loop repeating it 36 times is not degrading gracefully, it is failing 36 times and reporting a number that means the opposite.

`LLMError` could not tell the two apart, by design: the gateway is the single choke point and `LLMError` is the whole contract callers write fallbacks against (0003). Every one of those fallbacks is still correct here. Search should still return literal matches, `_name_clusters` should still fall back to member names, a correspondent should still file a week it could not embed. Only the batch loops need to know the difference.

## Decision

`LLMUnavailable(LLMError)`. A subclass, so no degrading caller changes at all, and the ones that need to stop can say so.

`_as_llm_error` raises it for `httpx.ConnectError` and `httpx.ConnectTimeout`, and nothing else.

- A **read timeout** is the opposite case. The server accepted the work and is still on it, which against a 21.7 GB model on a 10 GB card is ordinary, not exceptional (see 0003, and the writer's 600s cap). Treating that as an outage would abandon a run every time the router swapped a model in.
- A **mid-response disconnect** is ambiguous. A keep-alive socket closed by a healthy server looks identical from this side. It stays a per-item failure, and the next connect against a server that really died is what stops the loop. That costs one wasted attempt, which is what the 09-04 trace shows: one `RemoteProtocolError`, then connect failures forever.

Six handlers let it through today, in `triage`, `write`, `qa`, `summarize`, `narrate` and the two topic-resolution helpers, plus `agent._dispatch`, where a tool that reaches a model has the same problem the loop does: telling the model to route around a dead endpoint asks it to spend a turn on a server that will not answer that turn either.

`run_pipeline` catches it around the stage call, stops, and records `skipped` with the stage name. It does not try the next stage, because every remaining stage would empty its own queue against the same closed port and report 0. `pipeline_stage` (the `POST /api/jobs/defer/{stage}` path) records `skipped` on the run row and still raises, so the procrastinate job is visibly failed.

## Rejected

**A per-stage circuit breaker: stop after N consecutive failures.** It fixes the 0.9-second queue burn without needing a new type, and it also fires on N genuine per-item failures, which is a different thing that should not stop the run. It guesses at what the gateway already knows for certain.

**Re-probing the endpoint after each stage.** One place to edit instead of six, and it fixes the run status. It does not stop a stage from running its queue out, and it leaves `POST /api/jobs/defer/summarize` reporting success against a dead server.

**Making `LLMUnavailable` a separate exception, not an `LLMError`.** Then no loop could swallow it by accident, which is structurally stronger. It also breaks every degrade site in the codebase: a dead embed endpoint would fail a Matsedel scrape that has a perfectly good menu in hand. The contract in CLAUDE.md is load-bearing, and this would quietly repeal it.

## The convention, and what stops it rotting

Six `except LLMUnavailable: raise` lines are a convention, and a convention that lives in six places is six places to forget it. `tests/test_endpoint_gone.py` parametrizes over `STAGE_RUNNERS` and reads each stage's AST: a `try` that catches `LLMError`, `Exception` or everything must name `LLMUnavailable` first. A stage added later with a per-item handler and no re-raise fails the suite.

Checked by removing the `summarize` re-raise: the AST test fails, and so does the behavioural one, which puts three due posts through a gateway that refuses to connect and asserts the loop raises after the first. Without the re-raise it logs three warnings and returns 0, which is run 208 in miniature.

## Consequences

- A night llama-server is down now produces `skipped` rather than `succeeded`, at whatever stage it dies. `skipped` was already the pre-flight probe's word for the same condition, reached earlier.
- Work is never lost, only stopped: selection stays data-driven and the next run picks up the same rows.
- A run that dies during `embed` skips `score` too, so the feed is not rescored that night. Acceptable, and the alternative is a stage-by-stage list of which ones need a model, which is one more thing to keep true.
- `narrate` carries the re-raise for a call it does not yet make. `script.build_script` is the seam an LLM preprocessing pass replaces (0035), and the handler is correct before that lands rather than after.
