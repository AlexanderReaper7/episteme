# 0060. The warden client reads InferMux, and the lifecycle stays there

- Date: 2026-10-08
- Status: built and unit-tested 2026-10-08
- Supersedes: [0041](0041-the-host-agent-applies-a-configuration-it-is-handed.md); the parts of [0034](0034-logs-stream-as-byte-offset-deltas.md) that served llama.cpp's log
- Rule: `llm/warden.py` asks InferMux for `/warden/verdict`, `/warden/resources` and `/running`, and can `POST /warden/unload`, with the process's own key (0058). `LLM_WARDEN_URL` is InferMux's root. Starting, stopping, logs and model flags are InferMux's, in its UI, and Episteme does not copy them.

## Context

0058 left `llm/warden.py` speaking llama-warden's API: `/status`, `/start`, `/stop`, `/restart`, `/logs` and `/preset`. InferMux serves none of it, so after the move the client was switched off and the dashboard's backend panel, the log pane and the benchmark sweep were dead.

InferMux has equivalents for the reads. `/warden/verdict` carries `policy.gpu_busy_percent`, the threshold 0057 says Episteme must read rather than copy. `/warden/resources` has the same `foreign_gpu_percent`, `our_gpu_percent`, VRAM and `processes` fields, with `culprits` in place of `games_running`. llama-swap's `/running` lists the loaded models.

It has no equivalent for the rest, and on purpose. llama-swap starts a model on the first request for it and stops it on a TTL, so there is no server for a client to start. Its logs and its model files are in its own UI and in the nixcfg repository that holds its configuration.

## Decision

The user, 2026-10-08, chose:

- **Port the reads.** The GPU panel and the benchmark gate read `/warden/resources` and `/warden/verdict` as before.
- **Replace the lifecycle panel.** It lists `/running` and offers one action, an unload. The unload keeps the graceful composition the stop had: pause the pipeline, wait for the current unit, then unload, rolling the pause back if InferMux refuses. InferMux refuses while an interactive request is in flight and otherwise closes any batch session in flight, so without the wait it would cut a story off mid-write. An optional `LLM_WARDEN_UI_URL` links to InferMux's UI for logs and configuration.
- **Remove the sweep scenario.** It rewrote llama-server's preset through the warden and restarted it per variant (0041). Model flags now live in InferMux's model files, under version control on the host, so a second writer of them from Episteme would be two sources of truth. A sweep, if one is wanted again, belongs in InferMux.

## Rejected

- **Keep the client off.** The GPU panel and the benchmark gate are reads InferMux already serves. With the client off, the gate had no measurement and let every run start unsensed.
- **Proxy InferMux's logs into the dashboard.** InferMux's UI already shows them, and a copy here would need the offset protocol of 0034 on an API InferMux does not have.

## Consequences

- Old sweep runs still render, from their samples' `variant` labels and the `executor` column. A sweep run still queued fails with "Unknown scenario" rather than staying queued.
- `gateway.unload_models`, which the contention path calls when a pause arrives, still posts to llama-server's `/models/unload`. InferMux reports `status.value` and passes that route through, but counts it as ordinary traffic from the worker's batch key, so it refuses it while the GPU is yielded, which is the only time the contention path asks. Watched 2026-10-08 17:27: `Unload of Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M failed: ... batch requests wait while the GPU is yielded`. InferMux unloads on its own when it yields, so nothing is held that should not be, and the unload is redundant there rather than harmful.
- The benchmark gate and `contaminated` refuse or flag a run next to a game from `games_running`, which llama-warden read from Windows' Game Bar catalogue. InferMux has no game list (`priority` is a configured process list, `obs` here), so that half of the gate never fires now, and it decides on foreign load alone, as the warden does.
