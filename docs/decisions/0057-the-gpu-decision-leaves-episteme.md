# 0057. The decision to yield the GPU leaves Episteme

- Date: 2026-09-13
- Status: live-verified 2026-09-13 (below), except the warden-driven resume
- Rule: Episteme does not decide when to stop for a game. It applies what llama-warden announces at `POST /api/pipeline/announce`, and `worker/contention.py` decides only what stopping *means*.
- Supersedes [0023](0023-the-host-agent-is-a-sensor-not-a-decision-maker.md) and takes the policy half of [0024](0024-the-governor-brakes-on-contention-not-presence.md). Both records stay, because the reason 0023 wanted the policy here is the useful part and it is still true.
- The other half of this decision lives in llama-warden's own `docs/decisions/0001-the-warden-decides-and-says-so.md` (`C:\selfhosting\llama-warden`). That one covers why the warden decides; this one covers what Episteme does with the verdict.

## Context

`hostagent/` was a 15-file FastAPI service inside this repository that ran on the host, outside compose, and was never imported by anything in `src/`. It measured the GPU; `worker/governor.py` read the measurement on a `*/2` cron and applied thresholds from `settings`.

Deciding who gets the graphics card is not a newsfeed's subject. The user asked for the agent to become its own project, and for the decision to go with it.

## Decision

The measuring process is the deciding process. Episteme is a **consumer** of a verdict it did not compute:

```
POST /api/pipeline/announce
{"action": "pause", "reason": "foreign GPU load 91% >= 25% (bf6)", "since": "...", "warden": "llama-warden"}
```

`worker/contention.py:apply_announcement` is the whole receiving end. Three rules survive from the governor unchanged, because none of them was ever about who owned the threshold: only a `RESOURCE` pause may be lifted, a pause does not take VRAM out from under a running story, and an interactive turn outranks the unload.

What **is** new is the separation the split creates. The warden decides *that* we should stop; this decides *what stopping means*, which for Episteme is pausing the pipeline and handing VRAM back if nothing is mid-story. A second consumer of the same card would do something else entirely with the identical message, and neither has to know about the other.

## What Episteme gives up, and it is not nothing

0023 put the policy here deliberately: **a sensor that dies leaves no opinion behind.** An absent agent meant "no information", never a stale flag. That property is gone. The warden pushes on transition and repeats every 300 s until the message lands, so a warden that dies while Episteme is paused leaves Episteme paused, waiting for a resume nobody will send. The reasoning for accepting that, and the lease that was recommended and declined, is in llama-warden's 0001.

What Episteme does about it is make the failure **visible rather than silent**. `contended_at` changed meaning: it used to be when we last measured contention, and it is now when we were last *told* the GPU is still busy, re-stamped by every re-announcement. It is a freshness clock. A pause whose `contended_at` stopped advancing is a dead warden, not a busy GPU, and the two were previously indistinguishable. `since` still records when the pause began and is never moved by a repeat, or the panel's "paused at 18:04" would read "paused just now" every time anyone looked.

Idempotency is therefore a **cross-process contract**, not an implementation detail. `tests/test_contention.py` pins each branch: a repeat re-stamps only the freshness clock, a repeat against a hand-set pause never re-authors it as `RESOURCE`, a resume of a `MANUAL` pause is refused, and an action that is neither word is a 422 rather than a guess. A 500 would read to the warden as "retry", and it would retry the same unusable word every 300 s forever.

## One threshold, owned where it is measured

`resource_gpu_busy_percent` and its four siblings are deleted from `config.py`. The only code here that still needed a "the GPU is busy" number is the benchmark gate, which refuses to measure on a contended card, and it now reads that number off the warden's `/verdict` (`bench/runner.py:_busy_percent`). Copying the warden's number into our settings would be two numbers that mean the same thing in two repositories, and the one that drifts is always the copy. That was 0024's argument and the split did not change it.

`Warden.verdict()` exists for this: the warden measures on its own 30 s thread and `/verdict` returns what that thread cached, so it costs nothing. `/resources` is still the ~3.5 s fresh sweep, and the docstrings now say which is which, because reaching for the expensive one out of habit is the mistake that is easy to make.

The bench gate stays **stricter than the warden about games**, deliberately: the warden treats a running game as context and decides on load alone, since a minimised game holds VRAM without using the card. A benchmark taken next to a running game is meaningless whatever the load reads.

A run boundary asks the warden **once**. `_environment()` returns the sweep and the threshold together, and skips the verdict entirely when the sweep came back `None`: a warden that cannot answer `/resources` will not answer `/verdict` either, and asking anyway put a second connect timeout on each end of every run. That was measured, not reasoned about: 30 s per test in `test_bench_runner.py` until the two reads were folded into one.

## What was deleted from this repository

`hostagent/` (15 files), `worker/governor.py`, `tests/test_hostagent*.py`, `tools/build_hostagent_ico.py`, the five `resource_*` settings, `RESOURCE_GOVERNOR_*` from `.env` and compose, and the `textual`/`pystray`/`pillow` dev dependencies, which existed only so the console's tests could import it.

`llm/host.py` became `llm/warden.py`, `HostAgent` became `Warden`, and `LLM_HOST_AGENT_URL` became `LLM_WARDEN_URL`. The client is still deliberately not the gateway (0023's reasoning, unchanged): lifecycle control must not be reachable from inside a completion call.

The mark was split rather than copied. `graphics/logo/build_svg.py` kept `episteme_icon` and lost `hostagent_icon`; the warden's copy kept the reverse. Each generator now renders one mark and contains only that mark's code, which is checkable: both SVGs re-render byte-identical after the split (`df97cbcf4c92308974f2c6d976e3c021` here), and that is what proves the deletion was confined to the other mark.

## Live verification

2026-09-13, against `docker compose build worker && docker compose up -d worker web`, with `WardogsClient-Win64-Shipping` holding the card throughout.

**The cron is gone, and the scheduler is alive to prove it.** `episteme.govern_resources` last ran at 17:48:00, on the `*/2` schedule the old worker registered. 17:50 and 17:52 came and went with no such row, while `scheduled_stalled_recovery` fired at 17:50:00 on its own cron. An empty queue would have proved nothing; a queue running everything except this one task proves the registration is gone.

**The receiving end, by hand.** Five `curl` calls at `POST /api/pipeline/announce`:

| sent | answered | `/api/status` pipeline |
|---|---|---|
| `{"action": "yield"}` | 422 `Unknown action 'yield'; expected 'pause' or 'resume'` | unchanged |
| `pause` | `{"applied": true, "paused": true, "worker_running": false, "unloaded_models": []}` | `since` and `contended_at` both 17:50:09.927947 |
| `pause` again | `{"applied": false, "paused": true, "detail": "already paused by resource"}` | `contended_at` 17:50:15.849241, `since` still 17:50:09.927947 |
| `resume` | `{"applied": true, "paused": false, "job_id": 128142}` | all four fields null |
| `resume` again | `{"applied": false, "paused": false, "detail": "not paused"}` | unchanged |

Job 128142 is `episteme.run_pipeline`, `succeeded`.

**Then the warden drove it, for real.** `uv run python -m warden --headless` on the host, and its own log:

```
19:51:33 INFO Verdict: PAUSE - foreign GPU load 74% >= 25%
19:51:33 INFO Consumer episteme took pause (foreign GPU load 74% >= 25%):
              {'applied': True, 'paused': True, 'worker_running': False, 'unloaded_models': []}
```

Episteme read `paused: true, reason: resource, since 17:51:33.477813` 2 ms later, and `/admin/jobs` drew "pipeline paused, by llama-warden, another program is using the GPU." Nothing in this repository measured anything or compared anything to a threshold.

**The 300 s repeat, unattended.** 280 s later `contended_at` moved on its own to 17:57:02.814517 and `since` did not move. That is the warden's `REANNOUNCE_SECONDS` loop, not a second `curl`, and it is the cross-process idempotency contract observed rather than argued.

**The warden killed mid-pause**, which is the risk this decision accepts. `Stop-Process -Force` on the listener; `/verdict` refused the connection; Episteme stayed `paused: true` with `contended_at` frozen at 17:57:02. Exactly the designed failure: the pipeline holds, and the only evidence is a clock that stopped.

**And recovered by a restart.** The replacement warden, started by the `LlamaWarden` scheduled task, is a different process with its own `since` of 18:01:33. It announced `pause` into an already-paused Episteme, which kept `since` at **17:51:33**, the original pause set by the process that died, and moved `contended_at` to 18:01:35. Two warden processes disagreeing about when the pause began, and the consumer's answer unchanged, is a better test of the contract than the hand-driven repeat was.

**The threshold crossing the container boundary.** From inside `web`, `bench/runner._environment()` returned `busy_percent 25.0` (the warden's `policy.gpu_busy_percent`, over `host.docker.internal:5003`) with a sweep reading `foreign 91.6%`, and `gate` refused: `foreign GPU load 92% >= 25%`. `/admin/partials/backend-resources` rendered `· ours 0% · yields at 25.0%` with the load marked `bad`. That partial is worth naming: it still read `settings.resource_gpu_busy_percent` after the deletion, a live `AttributeError` found by grep rather than by the suite, because no test rendered it. There is one now.

**One bug the suite could not see.** The scheduled task's first start died at `TypeError: AgentAPI.__init__() got an unexpected keyword argument 'resources'`, after the watch thread had already logged that it was watching. The warden's rule that the UI measures nothing was enforced by a test asserting `AgentAPI` has no `resources` field, and nothing checked that `__main__` had stopped passing one: `--headless` never reaches `_console_api`, so 81 green tests and every hand-run of the warden missed it. Fixed and covered in the warden's repository. The lesson is not about this call site, it is that a seam tested from one side only is tested from neither.

## Not verified

The **resume leg driven by the warden**, and therefore the 300 s quiet window, since the game ran throughout and stopping it to watch a timer was not worth it. The hand-driven resume above covers what Episteme does with the message; untested is the warden choosing to send it.

A **pipeline stage running and then being interrupted**: every pause here found `worker_running: false` and so unloaded nothing. 0024 left the same gap for the same reason, and it is still the one path where "a pause does not take VRAM out from under a running story" is a claim rather than an observation.

**`contended_at` is not on screen.** `_pipeline_status.html` draws `since`, so the freshness clock this decision leans on is visible in `/api/status` and nowhere a person looks. The stranded pause above was diagnosed with `curl`. Drawing "last heard from the warden N ago", and marking a pause stale past some multiple of 300 s, is what would make the claim true for an operator rather than for a script.
