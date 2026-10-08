# 0003. LLM access goes only through the gateway, and roles map to endpoints in config

- Date: 2026-07 (roles), 2026-08-01 (endpoints became config)
- Status: accepted
- Rule: ask for a role, never a model or a URL. `LLMError` is the whole error contract.

## Context

One GPU that loads models on demand, several call sites, and a topology that keeps changing: embeddings moved to their own server, roles get split, a model gets swapped. Any of that reaching into call sites would mean editing them all.

## Decision

Code asks for a *role*, `main` / `fast` / `embed`. Config maps roles to model names (`LLM_MODEL_*`) and to endpoints (`LLM_<ROLE>_BASE_URL`, falling back to `LLM_BASE_URL`). `endpoint_for(role)` resolves the URL, `endpoints()` groups roles by distinct URL, and one httpx client is cached per URL, so roles sharing a server share a connection pool. Nothing special-cases `embed`; that it sits alone on :5002 today is only what the defaults say.

Why the default puts it there, which is a host-side constraint rather than a gateway one: the router's `--models-max` counts models globally with no per-model exemption. It has to be capped at 1 so the main and fast models never share VRAM, and that cap would evict the embedder along with them. A dedicated always-resident server on :5002, CPU-only (`--n-gpu-layers 0`), is the cheapest way to exempt one model from a global cap. The gateway needs no knowledge of this: `LLM_EMBED_BASE_URL=""` folds embeds back into the router and nothing in the code changes.

Structured output is enforced twice: the JSON schema goes out as `response_format` (a llama.cpp grammar constraint) and the response is validated with pydantic, with repair-prompt retries. `llm/schemas.py` is the contract; `agent.request_validated` is the in-conversation variant.

Embeddings are truncated to `EMBEDDING_DIM` and re-normalized, so any model of at least that many dimensions works without a schema change (see 0004).

`LLMError` is the gateway's entire error contract. Transport failures are wrapped into it by `_as_llm_error`, which since 2026-07-30 also covers the response parse, because a 200 carrying unexpected JSON was the last route out.

## Rejected

Hardcoding which port holds VRAM. `unload_models` visits every endpoint and lets the server decide: only llama-server in *router* mode reports a per-model `status.value`, so a plain single-model server reports nothing loaded and is never sent an unload.

## Origin

The router stalled a 600s timeout swapping the fast model in. The raw `httpx.ReadTimeout` blew straight through `_name_clusters`' handler, which exists precisely so that a naming failure degrades to member names, and failed the whole job. Every caller writes its degradation against `except LLMError`, so an unwrapped transport error is the one failure mode that bypasses all of them.

## Consequences

Moving any role onto its own llama-server is an environment change, not a code change. Health (`unavailable_endpoints`), the admin role table and `unload_models` all derive from the resolved URLs, so there is one topology, not three.

Model swaps are measured at ~100s typical and up to 600s worst case (see [model-swap-cost.md](../model-swap-cost.md)). Timeouts against a single-GPU box that loads on demand are ordinary. Design for them.
