---
name: testing-small-batches
description: When testing the pipeline, cap expensive stages at 1-3 items, never full runs
metadata:
  type: feedback
---

When live-testing the Episteme pipeline, run 1-3 articles, not a full pass —
e.g. `POST /api/jobs/defer/write?limit=2`, `qa?limit=1`, or a targeted
`write?story_id=X` / `qa?post_id=Y`.

**Why:** a full run is ~an hour of GPU (agentic writes are minutes each) and was
twice aborted mid-run during testing (2026-07-18); small batches give the same
signal in minutes and the data-driven stages pick up the remainder later anyway.

**How to apply:** always pass `limit` (1-3) when deferring write/qa/triage for
verification purposes; full `run_pipeline` is for the nightly cron or when the
user explicitly asks.
