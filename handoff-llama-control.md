# Handoff: llama.cpp lifecycle + log integration

**Status: BUILT and live-verified 2026-08-01.** Written 2026-07-30 as a design
handoff; kept because §7 and §9 are measurements worth keeping and §3–§5 record
why the shape is what it is. **CLAUDE.md is now the authority on what exists** —
read it for the as-built description, the three defects found live, and the cost
budget. This section records only where the build *diverged* from the plan below.

**Superseded in part on 2026-09-13 (0057).** The agent left this repository and
became llama-warden at `C:\selfhosting\llama-warden`, taking the decision with
it, so `hostagent/llama_agent.py` in §1 below is now `src/warden/` over there.
Point 3, "pull, not push", is **reversed**: the warden pushes its verdict to
`/api/pipeline/announce` and Episteme no longer polls or holds a threshold. The
entries below are left as they were written, because the argument point 3 makes
for pull is the thing 0057 had to answer, and a rewritten record cannot be
argued with.

**Audience:** the next agent picking this up. Read [CLAUDE.md](CLAUDE.md) and
[episteme-architecture.md](episteme-architecture.md) first; this document assumes them.

## What changed versus this plan

1. **One agent, not two.** This document's control agent (§4b) and spec §7's
   "idle monitor" were specified separately but want the same privileges on the
   same box, so they are one process: `hostagent/llama_agent.py`.
2. **Option B was dropped.** §3 recommended a log bind mount *and* the agent. The
   agent serves `GET /logs` itself, so the mount would be a second transport for
   one panel, earning only "logs survive an agent that is down" — and when the
   agent is down the start button is gone too, so the panel degrades as one unit.
3. **Pull, not push.** Spec §7 had the agent flipping `processing_allowed` via
   the API. Episteme polls instead: the agent stays stateless and credential-free,
   every threshold lives in Episteme's config beside the rest of the tuning, and a
   dead agent leaves *no opinion* — today's behaviour — rather than a stale flag
   with nobody left to clear it.
4. **No SSE** (§4b listed `/logs/stream`). htmx polling is what every other live
   surface in `/admin` uses; a streaming transport for a 3-second refresh would be
   a second mechanism earning nothing.
5. **§4b's "bind 127.0.0.1 only … cost: unreachable from the container" worry was
   unfounded.** Docker Desktop proxies `host.docker.internal` from the host side —
   which is exactly why the containers already reach llama-server's own
   loopback-bound :5001. Verified: the container reads :5003 fine. Loopback-only
   is both safe and sufficient, so §5.3's auth question resolves to "no auth".
6. **The signal is contention, not presence** (§5.1 / spec §7 both assumed user
   idle time). User decision: other work takes priority only where Episteme would
   *noticeably* degrade it. Idle detection was dropped entirely.
7. **§6's acceptance criteria are met** except two that the environment could not
   produce — see the end of CLAUDE.md's entry. Criterion 4 ("two rapid starts do
   not launch two routers") **failed on the first live attempt** and is now the
   most valuable regression test in the set.

---

## 1. The ask

Episteme currently has no visibility into, and no control over, the LLM backend it
depends on entirely. The user starts it by hand:

```pwsh
cd C:\selfhosting\llama-cpp\ && .\launch-llama-v2.ps1
```

Wanted:

1. **View llama.cpp's logs** from Episteme (the admin dashboard is the natural home).
2. **Start llama.cpp** from Episteme, so a backend that is down is recoverable without
   leaving the app.
3. Better backend integration generally — load state, model residency, why a call is
   slow.

---

## 2. What exists today (verified live, 2026-07-29/30)

### Topology

| Port | Role | Process | Notes |
|---|---|---|---|
| 5001 | router (decode: `main` + `fast`) | `llama-server.exe` | `--models-max 1`, `--sleep-idle-seconds 1800`, lazy load |
| 5002 | dedicated embed server | separate `llama-server.exe` | `-ngl 0 --device none`, always resident, holds no VRAM |

Confirmed via `/props` on 5001: `{"role":"router","max_instances":1,"models_autoload":true,"build_info":"b9882-48719618e"}`.
Both ports were listening under distinct PIDs. Containers reach the host at
`host.docker.internal:{5001,5002}`.

### What Episteme already has

Everything here is real and working — **do not rebuild it**:

- [`llm/gateway.py`](src/episteme/llm/gateway.py) — `unavailable_endpoints()`,
  `list_models(url)`
  (raw router rows, which include per-model `status.value` of `loaded`/`unloaded` and
  the full argv the router launched the model with), `endpoint_status()` (per-URL
  health + inventory tagged with the roles it serves), `unload_models()`.
  **Since 2026-08-01 the role→endpoint map is config** (`LLM_<ROLE>_BASE_URL`), so
  "which port serves what" is no longer a constant anything can assume: a control
  agent must read `gateway.endpoints()`, not `settings.llm_base_url` plus
  `llm_embed_base_url`.
- `POST /api/llm/unload` — frees VRAM now, no pause.
- `/admin` renders an `llm` block: base URLs, role→model mapping, model list.
- `llm_calls` persists every call with `duration_ms`, tokens, stage, story — the
  measurements in §7 all came from there.

### What is missing

- **Logs.** llama-server writes to its console only. The launcher does not pass
  `--log-file`, and `Start-Process -WindowStyle Minimized` (used for the embed server)
  captures nothing. There is no artifact for anything to read.
- **Start.** Nothing can launch the backend.
- **Stop/restart.** `unload_models()` frees VRAM but leaves the server running; there
  is no way to stop or restart the process itself.

---

## 3. The hard constraint — read this before designing anything

**Episteme runs in Docker; llama.cpp runs on the Windows host.** A container cannot
start a host process, cannot signal one, and cannot read another process's console
output. No amount of code inside this repo changes that. Every design below is a way
of crossing that boundary, and picking one is the first real decision.

| Option | Verdict |
|---|---|
| **A. Host-side control agent** — a small HTTP service on the host that Episteme calls | **Recommended.** Only option that cleanly does start/stop *and* logs. Cost: a second thing the user must have running. |
| **B. Log file on a bind mount** — launcher gains `--log-file`, Episteme mounts the directory read-only | Good, and **worth doing regardless** (it is a prerequisite for A's log endpoint too). Solves logs only, not start. |
| **C. Run llama.cpp in a container** | Rejected on measurement, not assumption — see §9. GPU passthrough works fine; *generation* is 12-14% slower, and the model library would have to be duplicated into a Docker volume. |
| **D. Docker socket → `docker run` a privileged helper** | Rejected: enormous blast radius for a convenience feature, on a single-user box with no auth. |
| **E. Scheduled task / Windows service the app pokes via a flag file** | Works for start, but a filesystem-flag control channel is unobservable and racy. Only if the user rejects A. |

**Recommendation: B now, A on top of it.** B is small, independently useful, and
unblocks the log half of the ask. A then adds lifecycle without needing a second
transport for logs.

---

## 4. Recommended architecture

### 4a. Prerequisite: launcher writes a log file

**Outside this repo** — `C:\selfhosting\llama-cpp\launch-llama-v2.ps1`. Confirmed
present in this build: `--log-file FNAME  Log to file`.

- Add `--log-file` to **both** the router and the embed `Start-Process` argument lists.
- Suggested location: `C:\selfhosting\llama-cpp\logs\{router,embed}.log`.
- **Rotation is the open question.** llama-server does not rotate. A multi-hour
  pipeline run at verbose level will grow this without bound; decide truncate-on-start
  vs. external rotation with the user.

Two launcher quirks to know:

- `[switch]$Foreground = $true` — a switch defaulting to `$true`, so the script
  **blocks by default**. Anything automating it must pass `-Foreground:$false` or spawn
  it detached.
- The `server` subcommand starts the embed server on 5002 first, but only if nothing is
  already listening there. So "start" is idempotent-ish already, and the control agent
  should not duplicate that check.

### 4b. The host-side control agent

A single-file service on the host — Python/FastAPI is the obvious pick since the user
already has `uv`, but PowerShell's `Start-Job` + `HttpListener` is defensible if the
goal is zero new dependencies.

- **Bind `127.0.0.1` only.** It can start processes; it must not be reachable off-box.
- Suggested port **5003** (5001/5002 taken).
- Must survive Episteme restarts — it is the thing that starts Episteme's dependency,
  so it cannot live in the same lifecycle.

Proposed surface:

| Route | Purpose |
|---|---|
| `GET /status` | Are 5001/5002 listening; PIDs; uptime; which models are loaded |
| `POST /start` | Run the launcher detached; idempotent (no-op if already listening) |
| `POST /stop` | Terminate both servers |
| `POST /restart` | Stop, wait for ports to free, start |
| `GET /logs?tail=N` | Last N lines of the router (and embed) log |
| `GET /logs/stream` | SSE tail, for a live admin view |

### 4c. What to build inside Episteme

- **`src/episteme/llm/host.py`** — the client for the control agent. Keep it a
  *separate module from `gateway.py`*: the gateway is the inference choke point, and
  process lifecycle is a different concern that must not acquire the ability to
  silently start a GPU process from inside a completion call.
- **Config** (`config.py`, following the existing comment style):
  `llm_host_agent_url: str = ""` (empty = feature off, everything degrades to today's
  behaviour), `llm_host_agent_timeout_seconds`, `llm_log_tail_lines: int = 500`.
- **API** (`web/api.py`, alongside `POST /api/llm/unload`):
  `GET /api/llm/backend`, `POST /api/llm/backend/start|stop|restart`,
  `GET /api/llm/logs?tail=N`.
- **Admin UI** (`web/templates/admin/`): a backend panel in the existing `llm` block —
  per-port up/down, loaded model with residency, a start button when down, and a log
  pane. htmx polling, consistent with the rest of `/admin`; no SPA framework
  (user decision, see CLAUDE.md).

Every one of these must be a **no-op when `llm_host_agent_url` is empty**. The agent is
optional infrastructure; Episteme must run exactly as it does today without it.

---

## 5. Decisions for the user — do NOT self-authorize

Per CLAUDE.md ("Don't self-authorize known design debt"), surface these and let the
user choose:

1. **Auto-start, or button only?** Should a pipeline run that finds the backend down
   start it, or fail and wait for a human? Auto-start means a cron job can spin up a
   GPU process at 03:00 with nobody watching.
2. **Does `POST /stop` exist at all?** It can kill a model mid-generation, including
   one the user is using from another application.
3. **Auth on the control port.** Episteme is deliberately unauthenticated
   (single-user, decided constraint) — but that constraint was decided for a *reader
   app*, not for a process-spawning endpoint. Loopback-only may be enough; confirm.
4. **Log rotation** (§4a).
5. **Language/runtime for the agent** — Python+uv vs. pure PowerShell.
6. **Should `/admin` show the embed server's log too**, or router only?

---

## 6. Acceptance criteria

Live-verified, in the browser, per project convention:

- [ ] Backend down → `/admin` shows it down, per port, without stack traces or hangs.
- [ ] Start from `/admin` → both ports listening; a subsequent pipeline stage succeeds.
- [ ] Log pane shows real router output, including a model load, and updates live.
- [ ] Two rapid start requests do not launch two routers.
- [ ] `llm_host_agent_url=""` → every existing page and route behaves exactly as today.
- [ ] Control agent down but llama.cpp up → Episteme still works; the panel degrades to
      "control unavailable", inference unaffected.
- [ ] Log tailing does not read an unbounded file into memory.

---

## 7. Landmines found live (measured, not guessed)

These came out of a real `propose_topics` failure on 2026-07-29 and are the reason this
handoff exists.

- **A cold model swap can stall a request for 600 s with zero bytes returned.**
  `llm_calls` row 1323: `duration_ms=600006`, no tokens, `stage=topics`. `--models-max 1`
  means every `main`↔`fast` alternation is an evict-and-load, and the request that
  triggers it waits for the whole load. `llm_timeout_seconds` is 600, so this is the
  timeout firing, not a hang.
- **Measured latencies** (`llm_calls`, errors excluded):

  | role | kind | n | avg | max | tok/s |
  |---|---|---|---|---|---|
  | main | tool-chat | 609 | 65.6 s | 571.8 s | 4.8 |
  | fast | chat | 561 | 4.7 s | 570.4 s | 32.7 |
  | embed | embed | 141 | 3.0 s | 27.8 s | — |

  Both decode roles show a ~570 s maximum against single-digit-second medians. That
  bimodality **is** the swap cost, and it is the single largest performance lever in
  the system.
- **Cold vs warm, measured directly** on the fast model: 109.6 s for a three-word
  answer cold, 7.2 s warm.
- **`GET /metrics` does not work as expected in router mode** — it returns
  `400 "model name is missing from the request"`. Per-model metrics need the model
  named; do not build a dashboard on a bare `/metrics` scrape.
- **`llm_calls` is already the best backend telemetry in the system.** Before adding
  new instrumentation, check whether a query answers the question — the whole table
  exists because the gateway is a single choke point.
- **One oversized call can dominate a whole job.** The first `propose_topics` run spent
  570 s of its 620 s in a *single* `fast` call that generated 14 199 completion tokens
  (naming 734 clusters at once). Embedding and clustering 840 labels was only ~44 s of
  it. Output length, not input size, is the cost — batching that call cut generation
  roughly ninefold.
- **Fixed in this session, relevant context:** the gateway used to re-raise raw
  `httpx` exceptions, so a `ReadTimeout` from a swap stall bypassed every caller's
  `except LLMError` fallback and failed the entire job — where the handler existed
  precisely so a naming failure would degrade to member names.
  `gateway._as_llm_error` now wraps transport failures. Timeouts against a single-GPU
  box that loads on demand are **ordinary**; design for them.

---

## 8. Out of scope

- Changing model choices, quantization, or `models-preset.ini` tuning — host-side, and
  the user's call.
- Multi-GPU or remote inference hosts.
- Replacing the `--models-max 1` residency policy. It is a deliberate constraint
  (≤1 decode model in VRAM, embed pinned to system RAM); §7's swap cost is its known
  price, and renegotiating it is a separate conversation with the user.

---

## 9. Measured: host vs container (2026-08-01)

§3 rejected containerizing llama.cpp on reasoning alone. It has since been
measured, with a build- and CUDA-matched container so that only the platform
differs. Full method, controls, raw data and reproduction:
**[docs/llama-cpp-host-vs-docker.md](docs/llama-cpp-host-vs-docker.md)**.

Headline: GPU passthrough works, but **token generation is ~13% slower under
WSL2** on both decode models — and §7 already established that output length is
what costs. Prefill is faster, but only when a model is fully GPU-resident, which
`main` is not (+0.4% there). A **bind mount of the model directory is
disqualifying** (~150-250 MB/s, and warm is no better than cold: the 20 GB main
model costs +110s on *every* load); a named volume fixes that at the price of
duplicating the model library out of the host workflow.

Reading: keep `main`/`fast` on the host — a recurring 13% on the system's most
expensive operation is a bad trade for removing the §3 boundary, which a log file
plus a control agent solves once. Move `embed` in: CPU-only so it never pays the
GPU tax (measured **+5.8% faster**), always-resident so its load is paid once, and
since role→endpoint became config (`LLM_<ROLE>_BASE_URL`) it is an env change with
no code change. **Decision left to the user, not taken.**
