# 0040. Prefill is measured from the stream, by differencing, and stored as timestamps

- Date: 2026-08-15
- Status: accepted
- Rule: never store or plot llama-server's aggregate rates. Store cumulative counters as measured, difference them at read time.

## Context

The plan this feature came from asserted that prefill could not be observed over HTTP, only inferred from time-to-first-token, and that a within-prompt prefill curve would need llama-server's own logs. That was wrong, and disproving it is the reason the feature has the shape it has.

llama-server b9882 accepts `"return_progress": true` on a streaming completion and emits `prompt_progress` chunks *during* prefill:

```json
{"choices":[{"delta":{},"finish_reason":null}],
 "prompt_progress":{"total":7697,"cache":0,"processed":2048,"time_ms":4145}}
```

Those counters are **cumulative**, and that single fact is what the reducer exists to handle. Measured against the live router on 2026-08-15, 7697 cold tokens into Qwythos-9B:

| span | instantaneous tok/s |
|---|---|
| 81 → 2129 | 494 |
| 2129 → 4177 | 165 |
| 4177 → 6225 | 113 |
| 6225 → 7697 | 92 |
| what `timings.prompt_per_second` reported | **152.8** |

A fivefold fall inside one request, reported as one number the model stops achieving after the first 2k tokens. The old hand-run benchmark stored that number. Confirmed again on the first run through the finished feature (4481 synthetic tokens, same model): 466 tok/s at the start, 38 at the tail, aggregate 262.

This subsumes the earlier finding that a synthetic 4k prompt overstates prefill 3-4x. It is not merely that short prompts are unrepresentative; it is that *any* aggregate averages away the shape, and attention being quadratic means the shape is the answer.

## Decision

### Difference, do not divide

`bench/series.py:reduce_stream` walks `(elapsed_ms, chunk)` pairs and emits one point per *interval*:

```python
delta_tokens = processed - last_processed
delta_ms = time_ms - last_time_ms
if delta_tokens > 0 and delta_ms > 0:
    prefill.append([float(processed), round(delta_tokens * 1000.0 / delta_ms, 2)])
```

Two guards are load-bearing rather than defensive. The first event is `(0, 0)`, and some builds repeat the final count as a last chunk; either would divide by zero or plot an infinite rate.

The x coordinate is `processed`, the server's own cumulative count, not a running sum of deltas. It is authoritative and it is what `cache` is measured against, so a partially cached prompt lands where the work actually started.

### Decode counts the server's token index, never chunks

Under MTP a single chunk can carry several accepted tokens, and acceptance is around 95%. Counting chunks would understate generation by most of it. `timings.predicted_n` is the index, `reasoning_content` counts exactly like `content` (0009: Qwopus spends whole budgets there), and the tail point is always kept because the last bucket is the slowest part of the curve and dropping it would systematically flatter the model.

### Timestamps are stored, rates are derived

`decode_series` is `[[token_index, elapsed_ms], …]`, and `decode_rate` differences it when the page renders. A stored rate would bake the bucket size into the row forever, so changing the bucket later would make historical samples incomparable with new ones. Prefill is the exception and stores the rate, because its intervals are the server's, not ours: we do not choose when a `prompt_progress` chunk arrives, so there is no parameter to regret.

### Aggregation is totals over totals

`report.compare_rows` sums tokens, sums milliseconds, then divides once. Averaging per-sample rates weights a 2k repetition equally with a 19k one and produces a headline no individual measurement supports:

```
2000 tok / 1.0 s  and  19000 tok / 19.0 s
mean of ratios  = 1500 tok/s   (nothing ran this fast)
totals / totals = 1050 tok/s
```

Failures are counted beside the rate, never averaged into it. "The 27B produced four numbers and two timeouts" is the finding; a table that quietly averaged the four would report a healthy model.

### Overhead stays visible

`wall_ms - prefill_ms - decode_ms` is whatever llama.cpp's own clock cannot see: queueing, the router's model swap, HTTP. It is left recoverable rather than folded into a phase, and `load_ms` is a separate column measured as the gap before the first `prompt_progress` chunk of a cold model.

## Rejected

- **Time to first token as a prefill proxy.** It yields one number, the same aggregate, and cannot distinguish a slow prompt from a slow queue.
- **Parsing llama-server's log.** The data is in the response. Log scraping would tie the benchmark to a build's formatting and could not attribute a line to a request.
- **Storing an instantaneous rate for decode.** Freezes the bucket size into history.
- **A `warmup` boolean column on the sample.** It is derivable from `run.params["warmup"]` plus `rep == 0`, and a stored copy can disagree with the run that produced it. Note the subtlety the derivation gets right and a column would have got wrong: every ladder point is rep 0 and every one is a real measurement, so a sample with a `rung` is never a warmup.

## Consequences

- The prefill chart is the one thing no existing tool shows, and it is the shape that explains why a 19k writer call takes seven and a half minutes.
- Charts anchor the y axis at zero. A throughput chart scaled to its own minimum turns a 54-to-49 tok/s drift into a cliff, and this feature exists because a plausible-looking number was believed.
- The ladder plots measured `prompt_n`, never the requested rung. `truncate` cuts by characters, so a rung labelled 8192 routinely lands at 7697; plotting the label would draw a point where nothing was measured.
