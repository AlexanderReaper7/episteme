# Decision log

Why Episteme is built the way it is. One file per decision: the date, what was chosen, what was rejected, and what it was measured against.

These are records, not instructions. The *rule* a decision produced lives in [CLAUDE.md](../../CLAUDE.md), which is read every session; the reasoning lives here, which is read when someone asks "why is this like this" or is about to undo it. Grep this directory before changing something that looks arbitrary.

Nothing here is edited to reflect later changes. A decision that was replaced gets a `Status:` line pointing at the one that replaced it, and the old record stays, because the reason it was wrong is the useful part.

## Ingestion and sources

| # | decision | date |
|---|---|---|
| [0005](0005-politeness-and-the-source-access-policy.md) | Politeness is hard; blocked sources are worked around without raising volume | 2026-07-17 |
| [0006](0006-two-http-transport-modes.md) | Two HTTP transport modes, chosen per source | 2026-07-18 |

## LLM plumbing

| # | decision | date |
|---|---|---|
| [0003](0003-llm-access-only-through-the-gateway.md) | LLM access goes only through the gateway; roles map to endpoints in config | 2026-07 |
| [0004](0004-embeddings-truncated-to-1024-dimensions.md) | Embeddings are truncated to 1024 dimensions | 2026-07-16 |
| [0010](0010-transcripts-are-stored-as-deltas.md) | Tool-loop transcripts are stored as deltas | 2026-07-17 |
| [0027](0027-every-llm-authored-field-is-grammar-constrained.md) | Every LLM-authored field is grammar-constrained, including the chart spec | 2026-08-02 |
| [0041](0041-the-host-agent-applies-a-configuration-it-is-handed.md) | The host agent applies a configuration it is handed, and never decides one | 2026-08-15 |
| [0038](0038-chat-streams-over-sse-on-a-fourth-role.md) | Chat streams over SSE, on a fourth role, through the writer's tool loop | 2026-08-11 |

## The pipeline

| # | decision | date |
|---|---|---|
| [0007](0007-citations-are-built-from-the-database.md) | Citations are built from the database, never from LLM free text | 2026-07-17 |
| [0008](0008-the-writer-is-one-agentic-conversation.md) | The writer is one agentic conversation per story, with editorial authority | 2026-07-17 |
| [0009](0009-qa-reviews-the-rendered-page-with-vision.md) | QA reviews the rendered page, with vision, and may demote | 2026-07-17 |
| [0013](0013-rich-sections-are-a-typed-union-with-closed-set-media.md) | Sections are a typed union; media is a closed set | 2026-07-19 |
| [0025](0025-quizzes-are-mandatory-and-never-scored.md) | Every feature carries a quiz; the reader is never scored | 2026-08-02 |
| [0026](0026-qa-is-a-tool-harness.md) | QA edits by section index, and gets no network tools | 2026-08-02 |
| [0028](0028-the-qa-screenshot-is-conditional.md) | QA screenshots only charts and diagrams | 2026-08-02 |
| [0037](0037-a-reader-requested-story-outranks-the-pipeline.md) | A reader-requested story outranks the pipeline, and says so in a column | 2026-08-11 |

## The assistant

| # | decision | date |
|---|---|---|
| [0036](0036-the-assistant-proposes-the-user-disposes.md) | The assistant proposes, the reader disposes: a write tool never executes | 2026-08-11 |

## Recommendation

| # | decision | date |
|---|---|---|
| [0017](0017-the-feedback-log-is-canonical.md) | The feedback log is canonical; the profile is derived by full replay | 2026-07-29 |
| [0018](0018-topic-vocabulary-is-canonical-and-resolved-in-code.md) | The topic vocabulary is canonical and resolved in code | 2026-07-29 |
| [0019](0019-a-topics-slug-is-its-permanent-identity.md) | A topic's slug is assigned once and never moves | 2026-07-30 |
| [0020](0020-affinity-is-stored-freshness-is-applied-in-the-query.md) | Affinity is stored; freshness is applied in the query | 2026-07-29 |
| [0021](0021-manual-topic-weights-are-absolute-and-do-not-decay.md) | A hand-set topic weight is absolute and does not decay | 2026-07-30 |
| [0022](0022-rescore-passes-are-coalesced-by-a-database-lock.md) | Rescore passes are coalesced by a database lock | 2026-07-30 |
| [0030](0030-topic-feedback-direction-is-inherited.md) | Topic chips inherit the rating's direction; save is only a bookmark | 2026-08-03 |

## Data and jobs

| # | decision | date |
|---|---|---|
| [0011](0011-all-feed-content-is-a-post.md) | Every visible content unit is a `Post` | 2026-07-18 |
| [0012](0012-provenance-is-post-scoped-per-attempt.md) | Provenance is scoped to a post version, and to its attempt | 2026-07-18 |
| [0014](0014-alembic-with-a-manual-review-gate.md) | Alembic, gated by a marker a human deletes by hand | 2026-07-20 |
| [0015](0015-pre-migration-backup-lives-in-env-py.md) | The pre-migration backup lives in `env.py` | 2026-08-02 |
| [0016](0016-stalled-jobs-are-recovered-by-heartbeat.md) | Stalled jobs are swept by heartbeat | 2026-07-21 |
| [0032](0032-job-history-retention-is-tiered-by-class.md) | Job history is pruned by class; repeated jobs fold in the table | 2026-08-03 |

## Host, GPU, resources

| # | decision | date |
|---|---|---|
| [0023](0023-the-host-agent-is-a-sensor-not-a-decision-maker.md) | The host control agent is a sensor and actuator, never a decision-maker | 2026-08-01 |
| [0024](0024-the-governor-brakes-on-contention-not-presence.md) | The governor brakes on contention, not presence; a pause has an author | 2026-08-01 |
| [0034](0034-logs-stream-as-byte-offset-deltas.md) | Logs stream as byte-offset deltas | 2026-08-03 |
| [0042](0042-the-agent-owns-one-console-hidden-behind-a-tray-icon.md) | The host agent owns one console, born hidden, behind a tray icon | 2026-08-15 |

## Narration

| # | decision | date |
|---|---|---|
| [0035](0035-narration-streams-and-tees-to-disk.md) | Narration streams over a WebSocket and tees to disk; voices are DB rows | 2026-07-21 |

## Frontend

| # | decision | date |
|---|---|---|
| [0001](0001-server-rendered-htmx-no-spa.md) | Server-rendered Jinja2 plus htmx, no SPA framework | 2026-07 |
| [0002](0002-everything-is-a-plugin-surface.md) | Everything that grows one item at a time is a plugin surface | 2026-07 |
| [0029](0029-icons-are-a-generated-carbon-sprite.md) | Icons are one generated Carbon sprite | 2026-08-03 |
| [0031](0031-one-predicate-decides-the-body-and-its-validator.md) | One predicate decides both the response body and its cache validator | 2026-08-03 |
| [0033](0033-polling-fragments-answer-204-when-nothing-changed.md) | A polling fragment answers 204 when nothing changed | 2026-08-03 |

## Investigations

Longer measurement write-ups whose conclusions feed the decisions above:

- [../llama-cpp-host-vs-docker.md](../llama-cpp-host-vs-docker.md), 2026-08-01: should inference move into Docker? Generation is ~13% slower under WSL2, a model-dir bind mount is disqualifying, `embed` is the one role worth moving. Decision still open.
- [../icon-set-choice.md](../icon-set-choice.md): six icon sets measured on the real theme, licenses, and what Carbon costs. Feeds 0029.
- [../benchmarks/](../benchmarks/)

## Adding one

Next number, `NNNN-kebab-title.md`, and the same headings the existing ones use: Date, Status, Rule, Context, Decision, Rejected, Consequences. Keep the measurements, keep what was rejected and why. Then add the row here, and the *rule* to CLAUDE.md only if it has to fire without being looked up.
