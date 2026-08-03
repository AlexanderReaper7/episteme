# 0008. The writer is a single agentic conversation per story, with editorial authority

- Date: 2026-07-17 (Phase 2.5), extended 2026-07-19
- Status: accepted
- Rule: one main-model conversation per story: research tools, then the constrained draft, without leaving the conversation.

## Context

The first design split research from writing across two calls and two models. That throws away the research context before the prose is written, and it swaps models mid-story, which on one GPU costs a model load each way (see 0003, ~100s typical).

## Decision

`llm/agent.py` runs one main-model tool loop per story: `web_search`, `fetch_page`, `finish_research`, and `demote_story`. It ends with the grammar-constrained `PostDraft` turn **in the same conversation**, so the draft is written by the model that did the research, with the research still in context.

The `fast` model does triage and a condense pass in a batch **before** any main-model work, so sources are condensed exactly once and no story causes a mid-run model swap. A deterministic `min_write_chars` thin-gate stays as a backstop ahead of all of it.

`demote_story` is real editorial authority: the writer may decide a story does not deserve a feature and send it back to being an aggregate card.

Prompts carry guidance, not quotas. Asking for "3 to 5 sections" produces sections to fill the count.

## Rejected

- A separate research agent handing notes to a separate writer. Parked as an idea before this landed, and superseded by it.
- A sub-agent for individual sections such as charts (asked and decided 2026-08-02): a second conversation re-prefills the grounding set the writer already holds warm, and on a 10 GB card a parallel slot divides `--ctx-size` between KV caches. Constraining the grammar was the cheaper fix (see 0027).
- Context compaction inside the loop. llama.cpp's prompt cache only helps on a common prefix, so a monotonically growing conversation is the cheapest possible shape; any rewrite of history re-prefills from the edit point.

## The defect that shaped the tool table

On the first live run the writer ended its research by calling `demote_story` ("no need to demote"), because it was the only terminal-looking tool. That killed the feature. `finish_research(note)` now exists as the explicit "done researching" affordance, `demote_story`'s description states that it KILLS the feature, and the loop breaks to the draft on finish. Regression-tested in `test_agent.py`.

## Consequences

The writer's grounding set is fixed at the end of its own research, which is what lets QA be denied network tools entirely (see 0026).
