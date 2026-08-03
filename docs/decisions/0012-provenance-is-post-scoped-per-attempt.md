# 0012. Provenance is scoped to a post version, and to the attempt that produced it

- Date: 2026-07-18 (post-scoped), 2026-07-19 (attempt ids and pins)
- Status: accepted
- Rule: a post version's provenance page shows only the calls that produced *that* version. A failed attempt's calls are never swept into a later one.

## Context

A rewrite archives the old post and produces a new one. If provenance is scoped to the story, every version shows every version's calls, and the page stops answering "how was this made".

Worse, a failed generation leaves calls behind. Stamping "the story's recent unstamped calls" onto whatever post lands next attributes work to an article that did not come from it.

## Decision

`llm_calls.post_id` records which post generation a call produced. QA stamps it at call time. Condense and write calls run before the post row exists, so `pipeline._stamp_post_calls` stamps them immediately after it lands. Triage stays `post_id NULL`, because it is genuinely story-level and belongs on every version's page. `POST_SCOPED_STAGES` in `models.py` is the single definition of that split, so the prune and the API cannot drift.

Write-side calls also carry a per-generation `attempt_id`, a uuid stamped at call time via `llm_context`. `_stamp_post_calls` targets exactly that attempt, so a failed attempt's calls stay attempt-tagged with `post_id` NULL, visible only in story-level history at `/api/stories/{id}/llm-calls`. The bootstrap timestamp backfill is guarded with `attempt_id IS NULL` for the same reason.

## Consequences

Retention prunes archived posts and, by age, their calls, past `llm_log_retention_days`, **unless pinned**. `posts.pinned` keeps a version plus its complete provenance page. `llm_calls.pinned`, set per attempt via `POST /api/llm-calls/pin`, protects a failed attempt's unstamped calls, which no post pin can reach.

Post 247's data was repaired in place when this landed: job 1822's 7 calls were unstamped and tagged `repair-job1822-...`.
