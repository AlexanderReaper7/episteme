# 0041. The host agent applies a configuration it is handed, and never decides one

- Date: 2026-08-15
- Status: accepted
- Amends: [0023](0023-the-host-agent-is-a-sensor-not-a-decision-maker.md), which said sensor and actuator. Editing a config file is an actuator's job; this record draws the line.
- Rule: the desired configuration lives in Episteme. `/preset` reads, `/preset/apply` writes what it is handed, `/restart` takes explicit `extra_args`. The agent chooses nothing and remembers nothing.

## Context

A flag sweep is the one benchmark scenario the worker cannot execute. Comparing `fit = on` against a hard `ngl` override, or two `--ctx-size` values, requires llama-server to be restarted with different arguments, and only the host can do that. The proximate motivation is real: an explicit `n_gpu_layers` in `models-preset.ini` silently defeated `fit = on` for months, because an explicit value is a hard constraint the fit solver may not violate, so it aborted and every layer was forced onto a 10 GB card. Removing it roughly tripled throughput. A sweep is how that stops being discoverable only by accident.

0023 forbids the agent holding *policy*. Whether editing an `.ini` counts as policy is the question, and it is not obvious in either direction: the file is a persistent statement of intent, which sounds like policy, but writing a value someone else chose is plainly an action.

## Decision

The line is **who chose the value**, not who wrote the file.

- `GET /preset` returns the file's text. Read-only, no interpretation.
- `POST /preset/apply` takes `{section: {key: value_or_None}}`, applies it, and returns the name of the backup it made.
- `POST /restart` takes `{"extra_args": [...]}`, passed through to the launcher.
- `POST /preset/restore` takes a backup name and puts the file back.

Every one of those is a verb with an object supplied by the caller. The agent has no defaults, no notion of a "good" configuration, and no memory of what it applied. `bench/runner.py` composes the variants, applies each, restarts, measures, and restores; if the worker dies mid-sweep the file is left as the last apply left it, and the backup on disk is the recovery, because the alternative (an agent that rolls back on a timeout) would be the agent holding an opinion about what should be running.

**The backup name is returned, never remembered.** The agent is stateless by construction and only the caller knows where a sweep began.

### Editing is line-based, and deletion comments out

The preset file's comments are its only history. A parse-and-rewrite through `configparser` would return a normalized file with every comment gone and every section reordered, which destroys the record of why a flag is set in exchange for nothing.

So `_edit_preset` works on lines, and sections are applied bottom-up:

```python
# bounds: section -> [start, end); applied bottom-up so an insertion
# never invalidates a later section's recorded bounds.
for section in sorted(overrides, key=lambda name: -bounds.get(name, [len(lines)])[0]):
```

Deleting a key comments it out rather than removing it, tagged so its origin is legible:

```python
lines[hit] = f"; [benchmark] {lines[hit].strip()}"
```

That is what makes "remove `ngl` and let `fit` solve" a reversible experiment a human can read afterwards, rather than a line that vanished.

### Two guards, because this endpoint writes to the host filesystem

Single-user forever means there is no auth anywhere, so loopback plus these two are the whole story:

- `_safe_args` allows `^[A-Za-z0-9_.:\\/=,+-]+$` per argument and raises 422 otherwise. No spaces, no quotes, no shell metacharacters reach a command line.
- `_backup_path` refuses anything not named `models-preset.ini.bak-*` inside `LLAMA_DIR`, so a restore cannot be aimed at an arbitrary file.

Both are tested against hostile inputs rather than assumed.

### A refactor the split forced

`/start` previously was the route. `/restart` needs to call it, and a FastAPI handler is a poor plain function: its parameter default is a `Body` marker object, not a value, so calling `start()` in-process yields `AttributeError: 'Body' object has no attribute 'get'`. Split into `start_servers(extra_args=None)` with the route as a thin wrapper. The existing concurrency tests call `start_servers` directly.

## Rejected

- **The agent picking a configuration** (a "benchmark mode", a known-good preset). That is exactly the policy 0023 forbids, and an agent that dies would leave the host in a state Episteme never chose.
- **`configparser` round-tripping.** Comments and ordering are the file's history.
- **Writing the preset from the worker over a bind mount.** It would put a host path in `docker-compose.yml`, and the restart still needs the agent, so the mount buys a second mechanism for half the job.
- **Rollback on a timeout inside the agent.** An opinion about what should be running, held by the component 0023 says holds none. The backup file is the recovery.

## Consequences

- A sweep is refused outright when `LLM_HOST_AGENT_URL` is unset, with that as the message. Everything else in `/admin/benchmarks` works without the agent; only `vram_free_mb` and the contention gate go quiet.
- Applying a variant is only legal at an item boundary, because a restart evicts every resident model. The runner therefore orders a sweep variant-major, and each variant pays one cold load per model.
- The host agent is now a configuration surface, which was the user's stated direction. 0023's rule survives intact: it still holds no thresholds and no defaults, and it still leaves no opinion behind when it dies.
