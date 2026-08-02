# Should llama.cpp run in Docker instead of on the host?

**Measured 2026-08-01** on the production box (RTX 3080 10 GB, 96 GB RAM,
Windows 11, Docker Desktop / WSL2). Supersedes the assumption in
[handoff-llama-control.md](../handoff-llama-control.md) §3, which rejected
containerizing llama.cpp on reasoning alone.

**The decision is still the user's.** This document is the evidence, not the ruling.

---

## Answer in one paragraph

Containerizing works — GPU passthrough is fine and needs no special effort. But
**token generation is ~13% slower under WSL2**, consistently, and generation is the
term that dominates this workload. Prefill is *faster* in a container, but only
when the model is fully GPU-resident, which the main model is not. Separately, a
**bind mount of the Windows model directory is disqualifying** (~150–250 MB/s, and
warm is no better than cold); a Docker named volume fixes that but means
duplicating the model library away from the host workflow. Net: keep `main` and
`fast` on the host. `embed` is the exception and is worth moving.

---

## Method, and the two confounds that had to be removed

`llama-bench`, three arms, **interleaved per round** so GPU thermals and ambient
desktop load hit every arm equally; medians over rounds, never single runs.

The first attempt produced a +35% prefill result for the container that was not
interpretable, because two variables moved at once:

1. **Build.** `ghcr.io/ggml-org/llama.cpp` publishes CUDA images only for scattered
   builds; none matched the host's b9882. Fixed by building llama.cpp at the host's
   exact commit (tag `b9882`, `48719618e`) — see
   [benchmarks/Dockerfile.cuda13](benchmarks/Dockerfile.cuda13).
2. **CUDA toolkit.** *Every* official image is CUDA **12.8**; the host install is
   **13.3**. A compute-bound kernel is exactly where a toolkit version shows up, so
   this alone could have explained the result. Fixed by building on
   `nvidia/cuda:13.3.0-devel`.

Measured toolkit effect turned out to be **≤1.4% everywhere** — it was not the
cause. It still had to be ruled out rather than assumed; the +35% would otherwise
have been reported as a platform property when nobody had shown that it was one.

Other controls:

- `-ngl` **pinned per model**. `--fit` sizes the offload to *currently free* VRAM,
  which drifts with the desktop, and two arms that loaded different layer counts
  would not be comparable.
- Free VRAM across arms agreed within **1.2%** (means 6802 / 6887 / 6871 MiB).
- Cold loads forced with `-dio 1` (direct I/O, bypasses the page cache), so "cold"
  is repeatable rather than a measure of how recently a file was touched.

### Known limits of these numbers

- `llama-bench` cannot drive speculative decoding, so the MTP draft settings in
  `models-preset.ini` are absent. Absolute t/s here is **not** production t/s; the
  host-vs-container *ratio* is the result.
- The media stack was running throughout, so both sides carry the same ambient
  CPU load.
- Container build is b9882 vs host b9882 — same commit — but the compilers differ
  (MSVC vs GCC 14.2). That is inherent to the comparison, not removable.

---

## Result 1 — throughput

Host b9882 / CUDA 13.3 vs container b9882 / CUDA 13.3. Only the platform differs.

| model | test | host | container | platform Δ | toolkit Δ |
|---|---|---|---|---|---|
| fast 9B (fully on GPU) | tg128 | 91.3 t/s | 78.3 t/s | **−14.3%** | +0.1% |
| fast 9B | pp512 | 1545 t/s | 1976 t/s | +27.9% | +0.6% |
| main 35B-A3B (10/48 layers) | tg128 | 24.7 t/s | 21.6 t/s | **−12.4%** | −1.2% |
| main 35B-A3B | pp512 | 368 t/s | 369 t/s | +0.4% | −1.4% |
| embed 4B (CPU-only) | pp512 | 130.3 t/s | 137.9 t/s | +5.8% | −1.4% |

**Generation is ~13% slower, on both decode models.** A decode step is a long chain
of very small kernel launches, which is precisely what WSL2's paravirtualized GPU
path taxes.

**Prefill does not pay that tax** — it is a few large compute-bound kernels — and is
faster in the container. But the gain only appears when the model is fully
GPU-resident: +27.9% on the 9B, +0.4% on the 35B, which at 10/48 layers is mostly
executing on the CPU.

Why the −13% is the number that matters: handoff §7 established from `llm_calls`
that **output length, not input size, is the cost** (one 14 199-token generation
consumed 570 s of a 620 s job). The penalty lands on the dominant term, and for
`main` — the model doing the agentic write loop — there is no offsetting prefill
gain. For `main`, containerizing is strictly worse on both axes.

## Result 2 — model load time

Seconds, and effective read throughput, for the same file.

| model | mode | host NTFS | container bind-mount | container named volume |
|---|---|---|---|---|
| embed 4.0 GB | cold | 1.9 s (2102 MB/s) | 26.5 s (154 MB/s) | 4.1 s (988 MB/s) |
| embed | warm | 1.2 s (3331 MB/s) | 21.6 s (189 MB/s) | 0.7 s (5662 MB/s) |
| fast 5.5 GB | cold | 1.5 s (3651 MB/s) | 22.7 s (247 MB/s) | 2.5 s (2283 MB/s) |
| fast | warm | 2.9 s (1964 MB/s) | 33.5 s (167 MB/s) | 5.4 s (1043 MB/s) |
| main 20.2 GB | cold | 11.6 s (1778 MB/s) | **121.4 s (171 MB/s)** | 26.2 s (790 MB/s) |
| main | warm | 4.5 s (4589 MB/s) | **108.5 s (191 MB/s)** | 7.8 s (2668 MB/s) |

**A bind mount of `C:\selfhosting\models` is disqualifying.** ~150–250 MB/s, and
critically **warm is no better than cold** — the Windows page cache does not bridge
the 9p boundary, so *every* load re-pays the full cost. The 20 GB main model costs
+110 s per load, on a swap that handoff §7 already calls the single largest
performance lever in the system.

**A named volume (ext4 inside the WSL2 VHDX) is viable**: 2.3× slower than host
cold (+15 s on the main model), ~+3 s warm. The price is that the model library
then lives inside Docker — duplicated from `C:\selfhosting\models`, and detached
from `models-preset.ini` and the launcher.

---

## Recommendation

**Keep `main` and `fast` on the host.** Moving them costs a permanent ~13% on
generation plus a duplicated 30 GB model library, and buys only the removal of the
host/container boundary described in handoff §3. Solving that boundary with a log
file plus a control agent is a one-time engineering cost; this would be a recurring
throughput cost on the system's most expensive operation.

**Move `embed` in.** It is the exception on every axis:

- CPU-only (`-ngl 0 --device none`), so it never pays the GPU-virtualization tax —
  and measured **+5.8% faster** containerized.
- 4 GB and always-resident, so its load cost is paid once at startup, not per swap.
- Holds no VRAM, so it is irrelevant to the `--models-max 1` residency policy.
- It removes one of the two processes currently started by hand, and lets compose
  own its lifecycle and logs.

Since 2026-08-01 the role→endpoint map is configuration
(`LLM_<ROLE>_BASE_URL`, resolved by `llm/gateway.py:endpoint_for`), so this is an
env change and **no code change** — point `LLM_EMBED_BASE_URL` at a compose service
instead of `host.docker.internal:5002`. Nothing in the codebase special-cases
`embed`; health, the admin role table and `unload_models` all derive from the
resolved URLs.

Not taken here — this is a decision for the user.

---

## Reproducing

```sh
# 1. Build the host-matched image (exact commit + CUDA 13.3)
docker build -f docs/benchmarks/Dockerfile.cuda13 -t llamabench:b9882-cuda13.3 docs/benchmarks

# 2. Throughput: host vs container, three arms, interleaved -> matched.jsonl
pwsh -NoProfile -File docs/benchmarks/bench-matched.ps1

# 3. Load time: host NTFS vs bind mount vs named volume -> load.jsonl
pwsh -NoProfile -File docs/benchmarks/bench-load.ps1

# 4. Clean up afterwards (the load benchmark stages ~30 GB into a volume)
docker volume rm llamabench-models
docker rmi llamabench:b9882-cuda13.3 nvidia/cuda:13.3.0-devel-ubuntu24.04
```

Raw measurements as produced: [benchmarks/matched.jsonl](benchmarks/matched.jsonl),
[benchmarks/load.jsonl](benchmarks/load.jsonl). Both scripts pin `-ngl`, record free
VRAM per run, and write one JSON line per `llama-bench` result row.

Note: the scripts hardcode `C:\selfhosting\models` and the host binary at
`C:\selfhosting\llama-cpp\llama-bench.exe`, matching this box.
