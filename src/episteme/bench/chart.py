"""Geometry for the inline SVG charts on /admin/benchmarks.

Pure, and on this side of the wire rather than in JavaScript, for the same reason
every other rendering decision in Episteme is server-side (0001): a chart drawn
by a library in the browser is a second data path that no test can see, and the
data here is at most a few hundred points that were already in the response.

The output is deliberately dumb - numbers and pre-joined `points` strings - so the
template is one `<polyline>` per line and no arithmetic. Everything that could be
wrong (a scale that collapses when every value is identical, an axis that starts
at a misleading non-zero, a tick sequence that reads as 0.30000000000000004) is
wrong HERE, where `tests/test_bench_report.py` can hold it - which it does, in
its second half, beside the aggregation those charts are drawn from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Enough hues to separate the models we actually compare (three, four with a
# variant) while staying inside the theme. Cycled rather than extended: a
# benchmark with nine lines on one axis is unreadable whatever the palette.
PALETTE = ("#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#7dcfff", "#f7768e")

PAD_LEFT = 52
PAD_RIGHT = 12
PAD_TOP = 12
PAD_BOTTOM = 30


@dataclass
class Line:
    label: str
    color: str
    points: str  # ready for <polyline points="…">
    last: tuple[float, float] | None = None  # data-space, for the end label


@dataclass
class Chart:
    width: int
    height: int
    lines: list[Line] = field(default_factory=list)
    x_ticks: list[tuple[float, str]] = field(default_factory=list)  # (px, label)
    y_ticks: list[tuple[float, str]] = field(default_factory=list)
    x_label: str = ""
    y_label: str = ""
    plot: tuple[float, float, float, float] = (0, 0, 0, 0)  # x0, y0, x1, y1 in px
    empty: bool = True


def _nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """A short sequence of round numbers spanning [low, high].

    The classic 1/2/5 decade walk. Worth the fifteen lines because the
    alternative, `low + i * (high - low) / n`, produces axis labels like
    "13.7 / 27.4 / 41.1" that the eye cannot use to read a value off the line -
    which is the only thing an axis is for.
    """
    if high <= low:
        return [low]
    span = high - low
    step = 10 ** math.floor(math.log10(span / count))
    for multiple in (1, 2, 5, 10):
        if span / (step * multiple) <= count:
            step *= multiple
            break
    start = math.ceil(low / step) * step
    ticks: list[float] = []
    value = start
    while value <= high + step / 1000 and len(ticks) <= count + 1:
        ticks.append(round(value, 10))
        value += step
    return ticks or [low, high]


def _format(value: float) -> str:
    if value >= 10000:
        return f"{value / 1000:g}k"
    if value >= 10 or value == int(value):
        return f"{value:.0f}"
    return f"{value:.1f}"


def line_chart(
    series: list[tuple[str, list[list[float]]]],
    *,
    x_label: str = "",
    y_label: str = "",
    width: int = 620,
    height: int = 240,
    y_from_zero: bool = True,
) -> Chart:
    """One chart from `[(label, [[x, y], ...]), ...]`.

    `y_from_zero` defaults true and that is a measurement decision, not a visual
    one: throughput charts anchored at their own minimum turn a 54-to-49 tok/s
    drift into a cliff, and this whole feature exists because a plausible-looking
    number was believed. The ladder passes it too. Nothing currently turns it off;
    the parameter exists so that a future latency chart, where zero is not a
    meaningful floor, can say so explicitly rather than by reaching in here.
    """
    chart = Chart(width=width, height=height, x_label=x_label, y_label=y_label)
    x0, y0 = PAD_LEFT, PAD_TOP
    x1, y1 = width - PAD_RIGHT, height - PAD_BOTTOM
    chart.plot = (x0, y0, x1, y1)

    points = [(float(p[0]), float(p[1])) for _, data in series for p in data if len(p) >= 2]
    if not points:
        return chart

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min = 0.0 if y_from_zero else min(ys)
    y_max = max(ys)
    # A single point, or a perfectly flat line, has zero span in one axis, and
    # dividing by it would put every point at the same pixel or raise. Padding
    # the range draws the line where it belongs: across the middle.
    if x_max - x_min < 1e-9:
        x_min, x_max = x_min - 1, x_max + 1
    if y_max - y_min < 1e-9:
        y_max = y_min + 1

    def px(x: float) -> float:
        return round(x0 + (x - x_min) / (x_max - x_min) * (x1 - x0), 1)

    def py(y: float) -> float:
        return round(y1 - (y - y_min) / (y_max - y_min) * (y1 - y0), 1)

    for index, (label, data) in enumerate(series):
        clean = [(float(p[0]), float(p[1])) for p in data if len(p) >= 2]
        if not clean:
            continue
        clean.sort(key=lambda point: point[0])
        chart.lines.append(
            Line(
                label=label,
                color=PALETTE[index % len(PALETTE)],
                points=" ".join(f"{px(x)},{py(y)}" for x, y in clean),
                last=clean[-1],
            )
        )
    chart.x_ticks = [(px(t), _format(t)) for t in _nice_ticks(x_min, x_max)]
    chart.y_ticks = [(py(t), _format(t)) for t in _nice_ticks(y_min, y_max)]
    chart.empty = not chart.lines
    return chart
