# 0059. Embeds go through InferMux

- Date: 2026-10-03
- Status: the InferMux side watched 2026-10-03; Episteme itself does not run on the NixOS host yet
- Rule: `LLM_EMBED_BASE_URL` defaults to empty, so the embed role uses `LLM_BASE_URL`, InferMux, with the process's key (0058). The CPU embedder stays its own always-resident llama-server; InferMux lists it as a peer named `embed`.

## Context

The embedder had its own URL, :5002, because the llama.cpp router's `--models-max 1` would have evicted it with every chat model swap (0003). InferMux replaced the router. A local model under it would be stopped on every GPU yield and refused to the batch worker during every pause, though it runs on the CPU.

## Decision

InferMux's 0011, the user's choice on 2026-10-03: the embedder stays `llama-embed.service` on :5002, and InferMux proxies to it as a llama-swap peer. A peer is not the card's tenant (InferMux 0009), so a pause passes the worker's embeds, a yield leaves the embedder running, and Episteme reaches every role at one URL with one key per process.

## Consequences

- An embed needs InferMux up, where before it needed only the embed server.
- InferMux checks the key and llama-server ignores it, as 0058 expected.
- Watched on the InferMux side: `/v1/embeddings` with the `episteme-batch` key returned 2560 dimensions, and returned 200 while the warden was yielded and a batch chat to a local model got 503.
