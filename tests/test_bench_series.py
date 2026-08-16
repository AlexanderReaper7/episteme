"""The stream reducer (`bench/series.py`).

The chunk shapes here are copied from a real llama-server b9882 response
captured on 2026-08-15, not invented: `prompt_progress` carrying CUMULATIVE
counters is the single fact this module exists to handle correctly, and a
fixture that guessed the shape would test the guess.
"""

from episteme.bench.series import decode_rate, reduce_stream, summarize, tok_s


def _progress(processed: int, total: int, time_ms: float) -> dict:
    return {
        "choices": [{"delta": {}, "finish_reason": None}],
        "prompt_progress": {"total": total, "cache": 0, "processed": processed, "time_ms": time_ms},
    }


def _token(text: str, predicted_n: int, prompt_n: int = 7697) -> dict:
    return {
        "choices": [{"delta": {"content": text}}],
        "timings": {"prompt_n": prompt_n, "predicted_n": predicted_n},
    }


def test_prefill_points_are_differences_not_cumulative_averages():
    """The measured case: 494 tok/s early, 92 tok/s late, and llama-server's own
    aggregate reporting 152.8 for the whole thing. Plotting the cumulative
    numbers would reproduce exactly the error this feature exists to expose."""
    events = [
        (0.0, _progress(0, 7697, 0)),
        (10.0, _progress(2048, 7697, 4145)),  # 2048 tokens in 4145 ms -> 494
        (30.0, _progress(4096, 7697, 12481)),  # 2048 in 8336 ms -> 246
        (70.0, _progress(7697, 7697, 34900)),  # 3601 in 22419 ms -> 161
    ]
    series = reduce_stream(events)
    assert [point[0] for point in series.prefill] == [2048.0, 4096.0, 7697.0]
    rates = [point[1] for point in series.prefill]
    assert rates[0] > 490 and rates[-1] < 165
    # Monotonically falling, which is the shape a cumulative average smears away.
    assert rates == sorted(rates, reverse=True)


def test_the_zero_event_and_a_repeated_final_count_produce_no_points():
    """Both guards are load-bearing: the first event is (0, 0) and some builds
    repeat the final count. Either would divide by zero or plot an infinite
    rate."""
    events = [
        (0.0, _progress(0, 512, 0)),
        (5.0, _progress(512, 512, 1000)),
        (5.1, _progress(512, 512, 1000)),
    ]
    assert reduce_stream(events).prefill == [[512.0, 512.0]]


def test_decode_uses_the_servers_token_index_not_the_chunk_count():
    """Under speculative decoding one chunk can carry several accepted tokens,
    and MTP accepts ~95 % of its drafts. Counting chunks would understate the
    generation rate by most of it."""
    events = [(float(i * 100), _token("x", predicted_n=i * 4)) for i in range(1, 21)]
    series = reduce_stream(events, bucket=32)
    assert series.decode_tokens == 80
    assert [point[0] for point in series.decode] == [32, 64, 80]


def test_the_tail_point_is_always_kept():
    """The last bucket is the slowest part of the curve. Dropping it because the
    generation ended mid-bucket would systematically flatter the model."""
    events = [(float(i * 10), _token("x", predicted_n=i)) for i in range(1, 40)]
    series = reduce_stream(events, bucket=32)
    assert series.decode[-1][0] == 39


def test_reasoning_content_counts_as_decode():
    """Qwopus puts its thinking in `reasoning_content`, which costs decode time
    exactly like prose (0009). Ignoring it reports the generation as empty."""
    chunk = {"choices": [{"delta": {"reasoning_content": "hmm"}}], "timings": {"predicted_n": 1}}
    assert reduce_stream([(1.0, chunk)]).decode_tokens == 1


def test_summarize_reads_llama_timings_and_keeps_overhead_visible():
    events = [
        (0.0, _progress(0, 100, 0)),
        (1.0, _progress(100, 100, 500)),
        (
            2.0,
            {
                "choices": [{"delta": {"content": "done"}}],
                "timings": {
                    "prompt_n": 100,
                    "prompt_ms": 500,
                    "cache_n": 0,
                    "predicted_n": 40,
                    "predicted_ms": 1200,
                    "draft_n": 100,
                    "draft_n_accepted": 94,
                },
            },
        ),
    ]
    summary = summarize(reduce_stream(events), wall_ms=2500)
    assert summary["prefill_ms"] == 500 and summary["decode_ms"] == 1200
    assert summary["accept_pct"] == 94.0
    # wall - prefill - decode = 800 ms of everything llama.cpp's own clock cannot
    # see. It has to stay recoverable rather than being folded into a phase.
    assert summary["wall_ms"] - summary["prefill_ms"] - summary["decode_ms"] == 800


def test_absent_draft_counters_mean_not_applicable_not_zero():
    chunk = {"choices": [{"delta": {"content": "a"}}], "timings": {"predicted_n": 1}}
    assert summarize(reduce_stream([(1.0, chunk)]), wall_ms=10)["accept_pct"] is None


def test_decode_rate_differences_stored_timestamps():
    """Timestamps are stored and the rate derived, so changing the bucket size
    later does not make historical samples incomparable."""
    assert decode_rate([[32, 1000], [64, 3000], [96, 5000]]) == [[64.0, 16.0], [96.0, 16.0]]
    assert decode_rate([[32, 1000]]) == []
    assert decode_rate(None) == []


def test_tok_s_refuses_a_meaningless_denominator():
    assert tok_s(100, 500) == 200.0
    assert tok_s(0, 500) is None
    assert tok_s(100, 0) is None
