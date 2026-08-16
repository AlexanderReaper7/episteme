"""Reduce a finished run's samples into the table and the charts.

Pure, taking plain dicts rather than ORM rows, so `/admin/benchmarks/{id}` is a
query and a render with no arithmetic between them - and so the aggregation can
be tested without a database. The rows come out of the web layer as mappings on
purpose; nothing here should be able to lazy-load.

**Totals, not means of ratios.** Prefill throughput for a group is
`sum(prompt_n) / sum(prefill_ms)`, never the average of each sample's tok/s.
Averaging ratios weights a 2k-token repetition the same as a 19k one, which is
how you get a headline number that no individual measurement supports - the exact
failure this whole feature exists to catch (0040).
"""

from __future__ import annotations

from .chart import Chart, line_chart
from .series import decode_rate, tok_s


def is_warmup(params: dict, sample: dict) -> bool:
    """Derived, not stored. `plan_items` marks rep 0 as the warmup whenever the
    run asked for one, so the run's own parameters plus the rep index already say
    it; a `warmup` column would be a second copy of a fact that can disagree."""
    if sample.get("rung") is not None:
        return False  # the ladder has one cold sample per rung, all of them real
    return bool(params.get("warmup", True)) and (sample.get("rep") or 0) == 0


def group_key(sample: dict) -> tuple[str, str]:
    return (sample.get("model") or "", sample.get("variant") or "")


def group_label(model: str, variant: str) -> str:
    return f"{model} · {variant}" if variant else model


def compare_rows(params: dict, samples: list[dict]) -> list[dict]:
    """One row per (model, variant), warmups and failures excluded from the rates.

    A failed sample still shows up as a count, because "the 27B produced four
    numbers and two timeouts" is the finding, and a table that quietly averaged
    the four would report a healthy model.
    """
    groups: dict[tuple[str, str], dict] = {}
    for sample in samples:
        key = group_key(sample)
        row = groups.setdefault(
            key,
            {
                "model": key[0],
                "variant": key[1],
                "label": group_label(*key),
                "n": 0,
                "errors": 0,
                "warmups": 0,
                "prompt_n": 0,
                "prefill_ms": 0,
                "decode_tokens": 0,
                "decode_ms": 0,
                "wall_ms": 0,
                "load_ms": None,
                "accept": [],
                "cache_n": 0,
            },
        )
        if sample.get("error"):
            row["errors"] += 1
            continue
        if sample.get("load_ms") and row["load_ms"] is None:
            row["load_ms"] = sample["load_ms"]
        if is_warmup(params, sample):
            row["warmups"] += 1
            continue
        row["n"] += 1
        for column in ("prompt_n", "prefill_ms", "decode_tokens", "decode_ms", "wall_ms", "cache_n"):
            row[column] += sample.get(column) or 0
        if sample.get("accept_pct") is not None:
            row["accept"].append(sample["accept_pct"])

    rows = []
    for row in groups.values():
        n = row["n"] or 1
        rows.append(
            row
            | {
                "prefill_tok_s": tok_s(row["prompt_n"], row["prefill_ms"]),
                "decode_tok_s": tok_s(row["decode_tokens"], row["decode_ms"]),
                "avg_prompt_n": round(row["prompt_n"] / n),
                "avg_wall_s": round(row["wall_ms"] / n / 1000, 1),
                "accept_pct": round(sum(row["accept"]) / len(row["accept"]), 1)
                if row["accept"]
                else None,
            }
        )
    return sorted(rows, key=lambda row: (row["model"], row["variant"]))


def ladder_series(samples: list[dict]) -> list[tuple[str, list[list[float]]]]:
    """Prefill throughput against real prompt length, one line per group.

    The x-axis is the server's measured `prompt_n`, never the rung the planner
    asked for: `fixtures.truncate` cuts by characters, so a rung labelled 8192
    routinely lands at 7.7k, and plotting the label would draw a point where no
    measurement was taken.
    """
    by_group: dict[tuple[str, str], list[list[float]]] = {}
    for sample in samples:
        if sample.get("error") or not sample.get("prompt_n"):
            continue
        rate = tok_s(sample.get("prompt_n"), sample.get("prefill_ms"))
        if rate is None:
            continue
        by_group.setdefault(group_key(sample), []).append([float(sample["prompt_n"]), rate])
    return [(group_label(*key), points) for key, points in sorted(by_group.items())]


def _representative(params: dict, samples: list[dict]) -> dict[tuple[str, str], dict]:
    """The sample whose curves stand for each group: the last real repetition.

    The LAST rather than the fastest or the mean. A mean of curves would need
    them resampled onto a common x-axis, which invents points; the fastest is the
    one least like a nightly run. The last non-warmup repetition is the steady
    state, which is what the nightly pipeline actually gets.
    """
    chosen: dict[tuple[str, str], dict] = {}
    for sample in samples:
        if sample.get("error") or is_warmup(params, sample):
            continue
        chosen[group_key(sample)] = sample
    return chosen


def run_charts(scenario: str, params: dict, samples: list[dict]) -> list[tuple[str, str, Chart]]:
    """`[(slug, caption, chart), ...]` for one run's detail page."""
    if scenario == "ladder":
        return [
            (
                "ladder",
                "Prefill throughput against prompt length. Each point is one cold "
                "prefill of a truncated fixture; the x-axis is what the server "
                "measured, not the rung requested.",
                line_chart(
                    ladder_series(samples),
                    x_label="prompt tokens",
                    y_label="prefill tok/s",
                ),
            )
        ]

    chosen = _representative(params, samples)
    prefill = [
        (group_label(*key), sample.get("prefill_series") or [])
        for key, sample in sorted(chosen.items())
    ]
    decode = [
        (group_label(*key), decode_rate(sample.get("decode_series")))
        for key, sample in sorted(chosen.items())
    ]
    return [
        (
            "prefill",
            "Instantaneous prefill rate through one prompt. Falling is normal and "
            "expected - attention is quadratic - and it is precisely what the "
            "server's own cumulative average hides.",
            line_chart(prefill, x_label="tokens processed", y_label="tok/s"),
        ),
        (
            "decode",
            "Instantaneous generation rate, bucketed. Measured at the reader's "
            "end of the socket, so it includes everything between the sampler and "
            "the browser.",
            line_chart(decode, x_label="tokens generated", y_label="tok/s"),
        ),
    ]
