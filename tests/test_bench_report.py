"""Aggregation and chart geometry - the two places a plausible wrong number could
still get onto the page after the reducer got it right."""

from episteme.bench.chart import line_chart
from episteme.bench.report import compare_rows, is_warmup, ladder_series, run_charts


def _sample(**over) -> dict:
    base = {
        "model": "big",
        "variant": "",
        "rep": 1,
        "rung": None,
        "prompt_n": 1000,
        "cache_n": 0,
        "prefill_ms": 1000,
        "decode_tokens": 100,
        "decode_ms": 1000,
        "wall_ms": 2100,
        "load_ms": None,
        "accept_pct": None,
        "error": None,
        "prefill_series": [],
        "decode_series": [],
    }
    return base | over


def test_rates_are_totals_over_totals_not_means_of_ratios():
    """Averaging per-sample rates weights a 2k repetition the same as a 19k one
    and produces a headline no individual measurement supports - which is the
    failure this whole feature exists to catch."""
    samples = [
        _sample(rep=1, prompt_n=2000, prefill_ms=1000),  # 2000 tok/s
        _sample(rep=2, prompt_n=19000, prefill_ms=19000),  # 1000 tok/s
    ]
    row = compare_rows({"warmup": False}, samples)[0]
    # Mean of the ratios would be 1500. Totals over totals is 21000/20000 s.
    assert row["prefill_tok_s"] == 1050.0


def test_the_warmup_is_excluded_from_the_rates_and_counted_beside_them():
    samples = [_sample(rep=0, prompt_n=1000, prefill_ms=4000), _sample(rep=1)]
    row = compare_rows({"warmup": True}, samples)[0]
    assert row["n"] == 1 and row["warmups"] == 1
    assert row["prefill_tok_s"] == 1000.0  # the slow first rep is not in it


def test_warmup_is_derived_from_the_runs_parameters_not_stored():
    """A `warmup` column would be a second copy of a fact the run row already
    holds, and the two can disagree."""
    assert is_warmup({"warmup": True}, _sample(rep=0)) is True
    assert is_warmup({"warmup": False}, _sample(rep=0)) is False
    # Every ladder point is rep 0 and every one of them is a real measurement.
    assert is_warmup({"warmup": True}, _sample(rep=0, rung=2048)) is False


def test_a_failed_sample_is_counted_never_averaged():
    """ "The 27B produced four numbers and two timeouts" is the finding. A table
    that quietly averaged the four would report a healthy model."""
    rows = compare_rows({"warmup": False}, [_sample(rep=1), _sample(rep=2, error="read timeout")])
    assert rows[0]["n"] == 1 and rows[0]["errors"] == 1


def test_variants_are_separate_rows_of_the_same_model():
    rows = compare_rows(
        {"warmup": False},
        [_sample(variant="fit-on"), _sample(variant="ngl-999", prefill_ms=3000)],
    )
    assert [row["label"] for row in rows] == ["big · fit-on", "big · ngl-999"]
    assert rows[0]["prefill_tok_s"] > rows[1]["prefill_tok_s"]


def test_the_ladder_plots_measured_prompt_length_not_the_rung_requested():
    """`truncate` cuts by characters, so a rung labelled 8192 routinely lands at
    7.7k. Plotting the label would draw a point where nothing was measured."""
    samples = [
        _sample(rep=0, rung=8192, prompt_n=7697, prefill_ms=7697),
        _sample(rep=0, rung=2048, prompt_n=1990, prefill_ms=1000),
    ]
    ((label, points),) = ladder_series(samples)
    assert label == "big"
    assert sorted(point[0] for point in points) == [1990.0, 7697.0]


def test_charts_are_scenario_shaped():
    ladder = run_charts("ladder", {}, [_sample(rep=0, rung=2048, prompt_n=1990)])
    assert [slug for slug, _, _ in ladder] == ["ladder"]
    longctx = run_charts(
        "longctx",
        {"warmup": False},
        [_sample(prefill_series=[[100, 400.0]], decode_series=[[32, 1000], [64, 3000]])],
    )
    assert [slug for slug, _, _ in longctx] == ["prefill", "decode"]
    assert not longctx[1][2].empty


def test_a_flat_line_still_draws():
    """Zero span in one axis would put every point on the same pixel, or divide
    by zero. A benchmark of a perfectly steady model must not blank the chart."""
    chart = line_chart([("m", [[0, 40.0], [100, 40.0]])])
    assert not chart.empty
    ys = {point.split(",")[1] for point in chart.lines[0].points.split()}
    assert len(ys) == 1  # horizontal, and inside the plot area


def test_a_single_point_draws_without_dividing_by_zero():
    chart = line_chart([("m", [[7697, 152.8]])])
    assert not chart.empty and chart.lines[0].points.count(",") == 1


def test_the_y_axis_starts_at_zero():
    """A throughput chart anchored at its own minimum turns a 54-to-49 tok/s
    drift into a cliff. This feature exists because a plausible-looking number
    was believed."""
    chart = line_chart([("m", [[0, 49.0], [1, 54.0]])])
    bottom = chart.plot[3]
    assert float(chart.lines[0].points.split()[0].split(",")[1]) < bottom
    assert chart.y_ticks[0][1] == "0"


def test_axis_labels_are_round_numbers():
    """ "13.7 / 27.4 / 41.1" cannot be used to read a value off a line, which is
    the only thing an axis is for."""
    chart = line_chart([("m", [[0, 0], [137, 41.1]])])
    assert [label for _, label in chart.y_ticks] == ["0", "20", "40"]
    assert [label for _, label in chart.x_ticks] == ["0", "50", "100"]


def test_no_data_is_empty_not_a_broken_chart():
    assert line_chart([]).empty
    assert line_chart([("m", [])]).empty
