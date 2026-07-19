---
name: two-stage-write-idea
description: User's idea (2026-07-19) to split write into research agent → writer agent; parked until pipeline visibility exists — do not implement on guesswork
metadata:
  type: project
---

On 2026-07-19 the user proposed splitting the write stage into two main-model
agentic loops: triage → **research agent** (searches/fetches, decides which
source texts to keep — curating the context) → **writer agent** (writes only
from that curated context). Motivation: worry about hallucinations and
information loss (writer currently drafts from fast-model condensations + tool
noise in one long conversation).

**Why:** context-quality concern, not a decided design — the user explicitly
said "we are mostly guessing here since there isn't sufficient visibility into
the pipeline while it's being executed."

**How to apply:** Do NOT implement until pipeline visibility is good enough to
show where quality is actually lost (writer's context at draft time, curated
vs. raw comparisons). The idea is recorded canonically in
episteme-architecture.md §13 Open Questions with trade-offs. If the user asks
about write-quality improvements or pipeline observability, connect it to this.
Related: [[testing-small-batches]], [[post-version-compare-idea]] (the
side-by-side compare view is the evaluation tool for this experiment).
