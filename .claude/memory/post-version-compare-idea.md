---
name: post-version-compare-idea
description: User's idea (2026-07-19) for a side-by-side post-version compare view with section-aware diff; parked — evaluation tool for pipeline experiments
metadata:
  type: project
---

On 2026-07-19 the user proposed a **side-by-side compare view for two post
versions of the same story, with a diff to visualize differences**. Post
versions already exist (a rewrite archives the old post with sections and
provenance intact; `attempt_id` groups calls per generation attempt), so the
data layer is done — only the view is missing.

**Why:** intended as the evaluation tool for pipeline-quality experiments,
especially [[two-stage-write-idea]] (research agent → writer agent) — compare
a story written under two pipeline configurations.

**How to apply:** Do NOT implement until asked. Recorded canonically in
episteme-architecture.md §13 (same bullet as the two-stage idea). Design notes
agreed: section-aware diff (align sections structurally, word-level diff
inside prose) rather than raw text diff; retention prunes archived versions
after `llm_log_retention_days` (raise for experiments); producing two
*candidate* versions pre-publish conflicts with "at most one published post
per story" and belongs with the planned quality-gate/draft-review push.
