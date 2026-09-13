# 0024. The resource governor brakes on contention, not on presence, and a pause has an author

- Date: 2026-08-01
- Status: half superseded by [0057](0057-the-gpu-decision-leaves-episteme.md), 2026-09-13. The policy (contention not presence, yield fast and resume slow, every threshold) left with llama-warden and is its 0001. What stayed here is the pause's AUTHOR: `RESOURCE` is still the only reason the warden may clear, and the four defects below are still the regressions `tests/test_contention.py` guards.
- Rule: `RESOURCE` is the only pause reason the governor may clear. Yield immediately, resume slowly.

## Context

The user games on the same machine. Other work takes priority, but only where Episteme would noticeably degrade it.

Idle-time detection was considered and dropped: someone typing an email is not a reason to stop writing articles.

## Decision

The governor polls `/resources` on a cron and owns every threshold. It pauses the pipeline when foreign GPU load crosses the configured percentage, and lifts that pause only after `resource_resume_quiet_seconds` of quiet.

The asymmetry is deliberate: yield immediately, resume slowly. Restarting a 20 GB model load during a lull between two loading screens is worse than waiting.

A pause records `reason` and `since` (`worker/control.py`). Two very different actors pause the pipeline, and without an author the governor would lift a pause a human set. `RESOURCE` is the only reason it may clear. A legacy `{"paused": true}` row reads as `MANUAL`, which is the safe direction.

"Stop" is composed rather than new: `POST /api/llm/backend/stop` sets the pause, waits for the worker to finish its current unit, and only then kills the processes. On timeout it returns `stopped: false` and leaves everything running; the panel then offers an explicit force. Nothing is killed mid-generation without a second deliberate click.

## Four defects found on review, 2026-08-01, all on paths the live run never reached

1. **The governor unloaded models mid-generation.** The pause is gentle precisely so the current story survives, and then `unload_models()` was called unconditionally, ripping the model out of VRAM under it. It now takes the same guard `/api/pipeline/pause` has, and that guard is now **one** predicate, `control.pipeline_job_running`, shared by both.
2. **The resume window timed the pause, not the quiet.** `since` was stamped once and never refreshed, so after 300s of a two-hour game the window had long expired and the first momentary dip, a loading screen or an alt-tab, resumed straight into it. The pause value now carries `contended_at` alongside `since`. `decide` returns a third action, `hold`, for "contended while already paused", and the caller re-stamps it via `control.mark_contended`, which never moves `since`, since that is what the panel shows.
3. **Two definitions of "holds VRAM" disagreed.** The governor tested `== "loaded"` while `unload_models` tested `not in (None, "unloaded")`, so during the ~100s a model takes to load, the governor attributed our own fresh allocation to someone else and paused and unloaded the model it was loading. `gateway.holds_vram(row)` is now the single predicate, and `loading` counts as loaded, because a model halfway into VRAM occupies it just as much.
4. **The graceful stop paused before finding out it could not stop anything.** An absent or unreachable agent, which is the normal state since it is optional, returned 503 *after* leaving the pipeline paused as MANUAL, which the governor is forbidden to lift, with no llama.cpp problem left to explain it. The agent is now pre-flighted before any write, and a failed kill rolls the flag back via `control.restore_pause`, a verbatim snapshot restore so a governor pause is not silently re-authored as a manual one. The *timeout* path still keeps the pause deliberately.

Also fixed: the `*/2` periodic is registered only when the governor is actually enabled, since 720 no-op job rows a day drowned the 20-row admin queue view; and `_backend.html` now shows the pause the stop button caused, because the pause controls live on `/admin/jobs`, a different page, so the operator previously got no indication at all on the one they were standing on.

## Live run, 2026-08-01, while Battlefield 6 was running

The panel read `foreign 90.7% / ours 0% / 863 MB free / games: bf6` and correctly showed both servers down. Start from `/admin` brought both up with PIDs and uptime. The governor paused for real (`reason: resource`, `foreign GPU load 90% >= 25% (bf6)`). Forcing the thresholds to read "quiet" against the *live* sensor confirmed all three resume cases: governor pause plus old contention resumed, governor plus recent waited out the window, manual was never touched. Graceful stop brought both down and left the pause set. Three concurrent starts produced exactly one launch and exactly 2 processes.

## Not verified live

A pipeline stage actually running after an agent-driven start, since the GPU was occupied by a game throughout and loading a 20 GB model on top of it is precisely what this feature exists to prevent. Also the graceful stop's *timeout* branch, since nothing was mid-generation to make it wait.
