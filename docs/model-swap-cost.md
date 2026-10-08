# What a model swap costs

**Measured 2026-07-29**, from a real `propose_topics` failure, on a single GPU that loads models on demand with at most one decode model resident (`--models-max 1`). These measurements shaped the llama.cpp lifecycle and log integration, and they are why the gateway treats a timeout as ordinary.

- **A cold model swap can stall a request for 600 s with zero bytes returned.** `llm_calls` row 1323: `duration_ms=600006`, no tokens, `stage=topics`. With one decode model resident, every `main`↔`fast` alternation is an evict-and-load, and the request that triggers it waits for the whole load. `llm_timeout_seconds` is 600, so this is the timeout firing, not a hang.
- **Measured latencies** (`llm_calls`, errors excluded):

  | role | kind | n | avg | max | tok/s |
  |---|---|---|---|---|---|
  | main | tool-chat | 609 | 65.6 s | 571.8 s | 4.8 |
  | fast | chat | 561 | 4.7 s | 570.4 s | 32.7 |
  | embed | embed | 141 | 3.0 s | 27.8 s | — |

  Both decode roles show a ~570 s maximum against single-digit-second medians. That bimodality **is** the swap cost, and it is the largest single performance lever in the system.
- **Cold against warm, measured directly** on the fast model: 109.6 s for a three-word answer cold, 7.2 s warm.
- **`GET /metrics` does not work as expected in router mode.** It returns `400 "model name is missing from the request"`. Per-model metrics need the model named, so a dashboard cannot be built on a bare `/metrics` scrape.
- **`llm_calls` is already the best backend telemetry in the system.** Before adding new instrumentation, check whether a query answers the question. The table exists because the gateway is a single choke point.
- **One oversized call can dominate a whole job.** The first `propose_topics` run spent 570 s of its 620 s in a *single* `fast` call that generated 14 199 completion tokens (naming 734 clusters at once). Embedding and clustering 840 labels took only ~44 s of it. Output length, not input size, is the cost: batching that call cut generation roughly ninefold.
- **The gateway used to re-raise raw `httpx` exceptions**, so a `ReadTimeout` from a swap stall bypassed every caller's `except LLMError` fallback and failed the entire job, even where the handler existed so that a naming failure would degrade to member names. `gateway._as_llm_error` now wraps transport failures. Timeouts against a single-GPU box that loads on demand are **ordinary**, and the code is designed for them.

Whether llama.cpp should move into a container was measured separately: [llama-cpp-host-vs-docker.md](llama-cpp-host-vs-docker.md).
