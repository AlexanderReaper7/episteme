"""The benchmark's own HTTP client for llama-server.

**Deliberately outside `llm/gateway.py` (0039).** The gateway hides the transport
from callers who are *using* a model, and enforces that they ask for a role
rather than a name. A benchmark is not using the model, it is measuring the
transport, so it sits outside the abstraction whose whole job is to make the
transport invisible. Putting a `model_override` on the gateway would weaken that
rule for all of its callers in order to serve this one.

Two properties follow, and they are the actual reason for the separation:

* **Nothing here writes `llm_calls`.** `llm/observe.py` is not imported and must
  not be. Otherwise provenance grows rows for posts that do not exist, and
  fixture capture (which selects prompts by percentile of real traffic) starts
  selecting benchmark traffic: benchmarks generating fixtures for benchmarks.
* **There is no tool loop.** Only `/v1/chat/completions`, never `agent.run_tool_loop`,
  because tools are precisely what a throughput measurement is trying to exclude.

Every request streams, which is not a preference:

* it is the only way to see inside prefill (`return_progress`, 0040), and
* it is the only way to cancel. llama-server aborts when the client disconnects,
  but a non-streaming request has nothing to notice a disconnect on until it
  tries to write its single response. The 2026-08-15 incident was exactly that:
  the client was killed and the server prefilled into nothing for ten more
  minutes, holding the card at 100 %.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from ..config import settings
from .series import StreamSeries, reduce_stream

log = logging.getLogger("episteme.bench.client")


class BenchError(Exception):
    """Anything that stopped a measurement. Deliberately not `LLMError`: nothing
    here degrades gracefully into a pipeline stage, and a caller catching
    `LLMError` must not accidentally swallow a benchmark failure."""


class Cancelled(BenchError):
    """The operator asked for the run to stop. Distinct from a failure, because a
    cancelled run keeps the samples it already finished."""


def _root(base_url: str) -> str:
    """llama-server serves `/props` and `/models/unload` at the server root while
    the OpenAI surface lives under `/v1`. Same split the gateway's unload path
    already deals with."""
    return base_url.rstrip("/").removesuffix("/v1")


class BenchClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        # No client-level timeout that could kill a legitimate 24-minute
        # generation: the per-request timeout below is explicit, and cancellation
        # is a first-class path rather than something a timeout stands in for.
        self._client = httpx.AsyncClient(timeout=None, headers=settings.llm_auth_headers())

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BenchClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- inventory ------------------------------------------------------------

    async def models(self) -> list[dict]:
        """Raw `/v1/models` rows. In router mode each carries `status.args`, the
        effective argv, which is the only column that can say WHAT changed when
        throughput moves."""
        try:
            response = await self._client.get(f"{self.base_url}/models", timeout=10.0)
            response.raise_for_status()
            return response.json().get("data", [])
        except Exception as exc:
            raise BenchError(f"listing models failed: {type(exc).__name__}: {exc}") from exc

    async def build(self) -> str | None:
        """llama.cpp build string. The router answers this without naming a model,
        unlike `/props`' other fields, which describe whichever model is loaded."""
        try:
            response = await self._client.get(f"{_root(self.base_url)}/props", timeout=10.0)
            response.raise_for_status()
            return response.json().get("build_info")
        except Exception as exc:
            log.debug("build probe failed: %s", exc)
            return None

    async def args_for(self, model: str) -> list[str] | None:
        for row in await self.models():
            if row.get("id") == model:
                return (row.get("status") or {}).get("args")
        return None

    async def is_loaded(self, model: str) -> bool:
        for row in await self.models():
            if row.get("id") == model:
                return (row.get("status") or {}).get("value") == "loaded"
        return False

    # --- the measurement ------------------------------------------------------

    async def measure(
        self,
        model: str,
        messages: list[dict],
        *,
        predict: int,
        cache_prompt: bool = False,
        temperature: float = 0.0,
        should_cancel: Callable[[], Awaitable[bool]] | None = None,
        on_progress: Callable[[dict], Awaitable[None]] | None = None,
    ) -> tuple[StreamSeries, float]:
        """Run one prompt through one model and return `(series, wall_ms)`.

        `temperature=0` because a benchmark wants the same work done twice, and
        `cache_prompt=False` because a rung of the ladder that reused a cached
        prefix would report a number nothing can reproduce. The result carries
        `cache_n` so that claim is checkable rather than trusted.
        """
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": predict,
            "temperature": temperature,
            "stream": True,
            # Cumulative prefill counters, per n_batch chunk (0040).
            "return_progress": True,
            # Puts a `timings` block on every chunk, which is where the token
            # index comes from: counting chunks undercounts under speculative
            # decoding, and MTP accepts ~95 % of its drafts.
            "timings_per_token": True,
            "cache_prompt": cache_prompt,
        }
        events: list[tuple[float, dict]] = []
        started = time.monotonic()
        # Cancellation and progress both cost a database round trip, so they are
        # time-throttled rather than run per chunk: at 40 tok/s a per-chunk check
        # would be 40 queries a second to answer a question whose answer changes
        # at human speed.
        next_poll = 0.0
        interval = settings.bench_poll_interval_seconds

        try:
            async with self._client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=httpx.Timeout(settings.bench_request_timeout_seconds, connect=30.0),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise BenchError(f"llama-server {response.status_code}: {body[:400]}")
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    body = line[6:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    events.append((elapsed_ms, chunk))

                    now = time.monotonic()
                    if now < next_poll:
                        continue
                    next_poll = now + interval
                    if on_progress is not None:
                        await on_progress(_snapshot(chunk, elapsed_ms))
                    if should_cancel is not None and await should_cancel():
                        # Leaving the `async with` closes the socket, which is
                        # what actually stops the GPU. Nothing else here can.
                        raise Cancelled(f"cancelled after {elapsed_ms / 1000:.0f}s")
        except Cancelled:
            raise
        except httpx.HTTPError as exc:
            raise BenchError(f"{type(exc).__name__}: {exc}") from exc

        wall_ms = (time.monotonic() - started) * 1000.0
        return reduce_stream(events, bucket=settings.bench_decode_bucket), wall_ms


def _snapshot(chunk: dict, elapsed_ms: float) -> dict:
    """One chunk reduced to what a progress pane can render.

    Prefill and decode are reported as separate phases because they are, and
    because the phase is the interesting part: a 19k writer call spends six of
    its seven minutes before the first token exists, and a pane that only counted
    tokens would show nothing at all for that whole stretch.
    """
    progress = chunk.get("prompt_progress")
    if isinstance(progress, dict):
        processed = progress.get("processed") or 0
        total = progress.get("total") or 0
        return {
            "phase": "prefill",
            "processed": processed,
            "total": total,
            "elapsed_ms": int(elapsed_ms),
        }
    timings = chunk.get("timings") if isinstance(chunk.get("timings"), dict) else {}
    return {
        "phase": "decode",
        "processed": int(timings.get("predicted_n") or 0),
        "total": 0,
        "prompt_n": int(timings.get("prompt_n") or 0),
        "elapsed_ms": int(elapsed_ms),
    }
