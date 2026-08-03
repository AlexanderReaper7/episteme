# 0011. Every visible content unit is a `Post`, including aggregate cards

- Date: 2026-07-18
- Status: accepted
- Rule: at most one published post per story. Aggregate cards are identity-only `Post` rows, not a second kind of thing.

## Context

The feed shows two kinds of card: a written feature, and a Google-News-style aggregate of a cluster nobody wrote about. Modelling the second as "a story with no post" meant every consumer needed a branch: the feed query, the provenance route, and, later, every feedback target.

## Decision

An aggregate card is a `Post` row with `kind="aggregate"` and NULL content columns. The card renders from the story's items at read time, so nothing is duplicated into it.

Triage mints the card on an `aggregate` verdict. A written feature archives it, so there is at most one published post per story. Every demote path, the writer's `demote_story`, the deterministic thin-gate, and QA, re-mints it through `pipeline.ensure_aggregate_post`. QA skips aggregates, which have no body.

## Consequences

Every visible unit has a `/post/{id}/provenance` page and, since Phase 4, a uniform feedback target. The feed query is one query over one table.

The NULL content columns bit back once: a keyword block written as `title.ilike(...)` returned NULL rather than false for every aggregate, and `WHERE` dropped them, so one blocked keyword silently hid 278 of 339 posts. `blocks.py` now wraps post-level columns in `COALESCE(col, '')`. See 0017.
