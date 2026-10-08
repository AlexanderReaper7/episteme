# 0058. InferMux knows the web from the worker by their keys

- Date: 2026-10-03
- Status: the worker side watched 2026-10-08: during a pipeline run, InferMux's `/warden/verdict` listed the worker's `/v1/embeddings` as `batch`. The web side is not watched yet.
- Rule: The web process and the worker send different InferMux keys, `episteme` (interactive) and `episteme-batch` (batch), from the file `LLM_API_KEY_FILE` names. Compose mounts one or the other at `/run/secrets/infermux-key`. Every worker job is batch, including the ones the reader deferred from the web.

## Context

llama-warden became InferMux (InferMux's own decisions 0004 and 0006): llama-swap's router with the warden inside, on :5001. It requires a key from every client once its `keys_file` is set, and the key carries the request's class. An interactive request is never cancelled or refused, and the models are not unloaded under it. A batch request is refused and cancelled while the GPU is contended. Until now Episteme sent no key and no class.

## Decision

The user, 2026-10-03, chose to classify "by who started it", then narrowed that for jobs the reader starts from the web UI: batch, so only the calls the web makes while an HTTP request waits are interactive. Those are chat, search, manual ingest and the feedback intent.

That makes the class a property of the process, so there is no per-call flag. Two keys in two files, and one setting that names the file. The web runs no background tasks, so nothing in it calls the LLM unless the reader is waiting.

The key is read when an HTTP client is built, and a configured file that does not exist fails that call. A silent fallback to no key would turn into a 401 on every call anyway, with a less useful message.

## Rejected

- **A reader-deferred job is interactive**, carried through its own procrastinate queue. A pressed "Run pipeline" is hours of LLM calls. As interactive, InferMux could not cancel them, and the unload would wait for all of it: one button press would hold the GPU against a game.
- **Only the single-target ones** (narrate, a stage on one post). One more rule to keep in step with every new task, for jobs short enough that a refusal costs little.

## Consequences

- The embed role still points at the separate embed server on :5002, which ignores the header. When the embedder moves under InferMux, the same key goes with it.
- `llm/warden.py` still talks to llama-warden's own API (`/status`, `/start`, `/logs`), which InferMux does not serve. That migration is separate, and is 0060.
