"""Throughput benchmarking (docs/benchmarks/plan.md).

Five modules, split by what is testable without a GPU:

* `series.py` - pure. Reduces one streamed completion into the prefill and
  decode curves. All of the interesting logic, none of the hardware.
* `chart.py` - pure. Turns stored curves into SVG geometry.
* `client.py` - HTTP to llama-server, outside the gateway on purpose (0039).
* `fixtures.py` - freezes real prompts out of `llm_calls`, since a synthetic
  prompt overstates prefill by 3-4x.
* `runner.py` - scenario planning (pure) and execution (not).
"""

from .chart import Chart, Line, line_chart
from .client import BenchClient, BenchError, Cancelled
from .runner import (
    SCENARIOS,
    BenchRefused,
    contaminated,
    create_run,
    gate,
    plan_items,
    run_benchmark,
)
from .series import StreamSeries, decode_rate, reduce_stream, summarize, tok_s

__all__ = [
    "SCENARIOS",
    "BenchClient",
    "BenchError",
    "BenchRefused",
    "Cancelled",
    "Chart",
    "Line",
    "StreamSeries",
    "contaminated",
    "create_run",
    "decode_rate",
    "gate",
    "line_chart",
    "plan_items",
    "reduce_stream",
    "run_benchmark",
    "summarize",
    "tok_s",
]
