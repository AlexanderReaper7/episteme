"""Turn one streamed completion into the two curves we store.

Pure, and deliberately so: it is the only part of benchmarking with interesting
logic, it has no GPU in it, and a reducer over a captured chunk list is testable
in milliseconds where the thing it measures costs minutes.

**The differencing is the whole point.** llama-server's `prompt_progress` events
carry CUMULATIVE counters, and the aggregate `timings.prompt_per_second` is a
cumulative average over the same numbers. Measured 2026-08-15 on 7697 cold
tokens: the server reported 152.8 tok/s while the instantaneous rate had already
fallen from 494 to 92. Plotting the cumulative figures would reproduce exactly
the error this feature exists to expose, so every point here is a delta against
its predecessor, in both series.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

# Per-token timings on a 16 tok/s model are dominated by chunk-boundary jitter,
# and a 2048-token generation is 2000 useless points. 32 is small enough that a
# real slowdown is still several buckets wide.
DECODE_BUCKET = 32


@dataclass
class StreamSeries:
    """What one request produced. Empty lists are a legitimate answer: a stream
    cut off during prefill has no decode curve, and a warm prompt has no prefill
    curve worth keeping."""

    prefill: list[list[float]] = field(default_factory=list)  # [[prompt_n, tok_s], ...]
    decode: list[list[int]] = field(default_factory=list)  # [[token_index, elapsed_ms], ...]
    timings: dict | None = None  # the final aggregate block, if the stream reached it
    decode_tokens: int = 0
    text: str = ""


def _content_of(chunk: dict) -> str:
    """The text a chat-completion chunk carries, if any. Reasoning models put
    their thinking in `reasoning_content`, which costs decode time exactly like
    prose does - a benchmark that ignored it would report a Qwopus generation as
    near-instant and empty (0009)."""
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        for key in ("content", "reasoning_content"):
            value = delta.get(key)
            if value:
                return value
    return ""


def reduce_stream(
    events: Iterable[tuple[float, dict]], bucket: int = DECODE_BUCKET
) -> StreamSeries:
    """Reduce `(elapsed_ms_at_receipt, parsed_chunk)` pairs into both curves.

    The caller timestamps, so this stays pure and a captured stream can be
    replayed in a test with its original timings. Two clocks are in play and each
    is used where it is the honest one: prefill points come from the server's own
    `time_ms`, because prefill happens entirely inside the server and our receipt
    time would only add transport noise; decode points come from OUR clock,
    because "when did the token reach the reader" is the thing the reader
    experiences.

    Token indices come from `timings.predicted_n` when the request asked for
    `timings_per_token`. Counting chunks instead would undercount under
    speculative decoding, where one chunk can carry several accepted draft
    tokens, which on an MTP model at 94 % acceptance is most of them.
    """
    series = StreamSeries()
    previous: tuple[int, float] | None = None  # (processed, time_ms), cumulative
    tokens = 0
    text: list[str] = []
    last_bucketed = -1

    for elapsed_ms, chunk in events:
        progress = chunk.get("prompt_progress")
        if isinstance(progress, dict):
            processed = progress.get("processed")
            time_ms = progress.get("time_ms")
            if processed is None or time_ms is None:
                continue
            if previous is not None:
                done = processed - previous[0]
                spent = time_ms - previous[1]
                # A repeated or non-advancing counter is not a measurement. Both
                # guards are load-bearing: the first event is (0, 0), and the
                # last one repeats the final count on some builds.
                if done > 0 and spent > 0:
                    series.prefill.append([float(processed), round(done * 1000.0 / spent, 2)])
            previous = (processed, float(time_ms))
            continue

        timings = chunk.get("timings")
        if isinstance(timings, dict):
            # Present on every chunk under `timings_per_token`, and on the final
            # chunk regardless. Keeping the latest means the aggregate we store
            # is the complete one even when the stream ends early.
            series.timings = timings

        content = _content_of(chunk)
        if not content:
            continue
        text.append(content)
        predicted = (timings or {}).get("predicted_n") if isinstance(timings, dict) else None
        tokens = int(predicted) if predicted else tokens + 1
        if tokens - last_bucketed >= bucket:
            series.decode.append([tokens, int(elapsed_ms)])
            last_bucketed = tokens

    # The tail matters more than any interior point: it is the slowest part of
    # the curve, and dropping it because the generation ended mid-bucket would
    # systematically flatter the model.
    if tokens and (not series.decode or series.decode[-1][0] != tokens):
        series.decode.append([tokens, int(elapsed_ms)])
    series.decode_tokens = tokens
    series.text = "".join(text)
    return series


def summarize(series: StreamSeries, wall_ms: float) -> dict:
    """The scalar columns of a `benchmark_sample`, from the same stream.

    Reads the aggregates out of `timings` rather than recomputing them from the
    curves: llama.cpp measures prefill and decode inside its own loop, where wall
    time cannot see queueing, HTTP or our own event loop. `wall_ms` is passed in
    precisely so the difference stays visible as overhead instead of being
    smeared into one of the two phases.
    """
    timings = series.timings or {}
    draft_n = timings.get("draft_n") or 0
    accepted = timings.get("draft_n_accepted")
    return {
        "prompt_n": int(timings.get("prompt_n") or 0),
        "cache_n": int(timings.get("cache_n") or 0),
        "prefill_ms": int(timings.get("prompt_ms") or 0),
        "decode_ms": int(timings.get("predicted_ms") or 0),
        # `predicted_n` is authoritative; our count is the fallback for a stream
        # that never delivered a timings block at all.
        "decode_tokens": int(timings.get("predicted_n") or series.decode_tokens),
        "wall_ms": int(wall_ms),
        # Speculative decoding only reports these when it actually ran, so an
        # absent draft_n is "not applicable", not zero acceptance.
        "accept_pct": round(100.0 * accepted / draft_n, 1)
        if draft_n and accepted is not None
        else None,
        "prefill_series": series.prefill,
        "decode_series": series.decode,
    }


def decode_rate(decode_series: list[list[int]] | None) -> list[list[float]]:
    """`[[token_index, elapsed_ms], ...]` to `[[token_index, tok_s], ...]`.

    Stored as timestamps and differenced here, rather than stored as rates,
    because the timestamps are the measurement and the rate is a view of it. A
    stored rate would bake the bucket size into the row forever: change
    `bench_decode_bucket` and every historical sample becomes incomparable with
    every new one, with nothing in the data to say so.
    """
    points: list[list[float]] = []
    previous: tuple[int, int] | None = None
    for entry in decode_series or []:
        if len(entry) < 2:
            continue
        tokens, elapsed = int(entry[0]), int(entry[1])
        if previous is not None:
            done = tokens - previous[0]
            spent = elapsed - previous[1]
            if done > 0 and spent > 0:
                points.append([float(tokens), round(done * 1000.0 / spent, 2)])
        previous = (tokens, elapsed)
    return points


def tok_s(tokens: int | None, ms: int | None) -> float | None:
    """tok/s, or None when the denominator makes it meaningless. Shared by the
    compare table and the charts so a zero-length phase renders as a blank cell
    everywhere rather than as 0.0, which reads as a measurement."""
    if not tokens or not ms:
        return None
    return round(tokens * 1000.0 / ms, 1)
