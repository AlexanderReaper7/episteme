# 0023. The host control agent is a sensor and an actuator, never a decision-maker

- Date: 2026-08-01
- Status: superseded by [0057](0057-the-gpu-decision-leaves-episteme.md), 2026-09-13. The agent left this repository and became llama-warden, taking the decision with it. The rule below is reversed: the process that measures is now the process that decides. Kept because the property it bought, a dead sensor leaving no opinion behind, is exactly what the split gives up, and 0057 is answerable to it.
- Rule: `/resources` reports measurements. `worker/governor.py` owns every threshold. An agent that dies leaves no opinion behind.
- Amended by [0041](0041-the-host-agent-applies-a-configuration-it-is-handed.md), 2026-08-15: the agent also edits `models-preset.ini` and restarts with explicit arguments. The rule above is unchanged, because the caller chooses every value; 0041 is where that line is drawn.
- Amended by [0042](0042-the-agent-owns-one-console-hidden-behind-a-tray-icon.md), 2026-08-15: it is no longer headless. It owns a console window and a tray icon, and both llama-server windows are gone. Still no policy.

## Context

llama.cpp runs on the host; Episteme runs in Docker. Starting, stopping and measuring the backend needs host privileges, and the thing that starts Episteme's dependency cannot share Episteme's lifecycle.

Two separately-specified features wanted the same privileges on the same box: the lifecycle controller of handoff-llama-control.md §4b, and the idle monitor of spec §7. They are one service.

## Decision

`hostagent/llama_agent.py`, a single-file FastAPI service on the host, loopback :5003, with a PEP-723 header so `uv run` resolves its two dependencies and there is no venv to maintain. Installed as a scheduled task starting at logon.

It reports measurements and performs actions. It holds **no policy**. Every threshold lives in Episteme's config, so an agent that dies leaves no stale flag behind, only an absence.

`llm/host.py` is the client, deliberately separate from `gateway.py`, so lifecycle control never becomes reachable from inside a completion call. `LLM_HOST_AGENT_URL` empty disables all of it and Episteme behaves exactly as before, which is verified rather than assumed.

## What can and cannot be measured, and where each is valid

- **Per-process GPU utilization** comes from the `\GPU Engine(*)\Utilization Percentage` performance counter, so `foreign_gpu_percent`, total minus our own llama-server PIDs, is truthful **even while we generate**. That is what makes it a pause signal and not merely a start gate.
- **VRAM cannot be attributed.** `nvidia-smi --query-compute-apps` returns `[N/A]` per process under WDDM, and the `GPU Process Memory` counter over-reports badly: measured, dwm claiming 22 GB on a 10 GB card, because it counts committed rather than resident. So free VRAM is consulted **only while our own models are unloaded**, where the whole figure is by definition someone else's.
- **Games** come from intersecting Windows' Game Bar registry (`HKCU:\System\GameConfigStore\Children`, 310 entries) with running processes, a self-maintaining watchlist. Reported as context for the panel, never decided on.

## Cost budget, measured, and it shaped the design

`Get-Counter` over all 621 GPU-engine instances is 3.3s, about 1s of which is PDH's own sampling floor, so `/resources` is ~3.5s. It is therefore never on a synchronous path: the admin panel loads it as its own htmx fragment and the governor polls it on a cron.

`Get-NetTCPConnection` was dropped entirely: **3.1s per fresh process**, because it re-imports NetTCPIP every time, against 259ms for bare PowerShell. Liveness is a Python socket connect instead, ~1ms and a truer test. `/status` fell 5.15s to 1.09s and costs zero subprocesses when the backend is down, which is exactly when someone is looking at it.

## Three defects found live, each regression-tested

1. **Two rapid starts produced four llama-server processes.** A bare "is it listening?" check is a TOCTOU race: both requests saw nothing bound and both ran the launcher. The launcher's own `Get-NetTCPConnection` guard has the identical hole. Every lifecycle route now takes a reentrant lock held **through the port wait**, not merely through the check, since releasing after spawning would let the next caller observe the not-yet-bound port and launch again. The test fails with 4 launches if the lock is removed.
2. **The log pane replaced the entire dashboard.** `.admin-main` carries `hx-target="#admin-main"` and **htmx inherits `hx-target`**, so a self-replacing fragment relying on the default target swallowed the page, then polled against an element it had deleted. Every partial here now names `hx-target="this"`. Worth remembering for any new admin fragment.
3. **ANSI escapes in the log file.** llama-server colors its output and `--log-file` receives the codes verbatim. Stripped in the agent rather than via `--log-colors off`, so the pane is correct however the server was started and a human running the launcher in a console keeps their colors.

Also fixed on review: `/start` verifies the ports actually bound rather than trusting the exit code, since PowerShell's default `$ErrorActionPreference` is Continue and a launcher that failed on a missing model exits 0. It reported `started: true` and discarded the captured output that explained it.

The client has **three** timeouts rather than one, because reads sit on the dashboard's critical path, where a hung agent blocked the page for the length of a model load, while `/restart` is a stop and a start in one request and could exceed a single ceiling, rendering a success as a failure.

## Launcher changes, outside this repo

Backup at `launch-llama-v2.ps1.bak`. `--log-file` and `--log-timestamps` on both servers; logs truncated on start, the user's choice since llama-server does not rotate; and a `-Detached` switch that starts hidden with no console, since a service-started process has none to attach to. `-Detached` wins over `-Foreground`, which defaults to `$true`.
