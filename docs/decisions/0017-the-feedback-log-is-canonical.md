# 0017. The feedback log is canonical; the interest profile is derived by full replay

- Date: 2026-07-29 (Phase 4)
- Status: accepted
- Rule: `interest_profile` is a cache. There is no incremental "apply this event" path, and there must never be one.

## Context

A learned profile that is mutated in place has two properties nobody wants: undo is approximate, and the constants it was trained under are frozen into it. Retuning a half-life then means the profile disagrees with what the reader actually did, in a way nothing can detect.

## Decision

The `feedback` table is canonical. `interest_profile` is produced by `profile.replay(events, now)`, a pure function, and rebuilt **in full** on every write. There is deliberately no incremental path, because that is precisely what would let the cache disagree with the log.

Each event carries its own payload: the embedding, and a snapshot of the topics and source at the time it was recorded. Replay therefore never reads mutable state. Stories keep absorbing items and posts are eventually pruned, and "what I reacted to" is not "what that story is now". `feedback.post_id` is `ON DELETE SET NULL` for the same reason.

## Consequences

- Undo is exact: delete the row, replay.
- Changing a half-life or a signal weight reinterprets the entire history.
- A full replay per write is affordable at this scale and is what makes the two properties above true, so the cost is the feature.

`blocks.py` holds the single definition of what a hard block excludes, shared by the feed query and the write queue so the two cannot drift.

## Three defects found in the first live run, 2026-07-29

1. **Keyword blocks emptied the feed of aggregates.** `NULL ILIKE …` is NULL, not false, so the predicate went NULL for every aggregate card, whose content columns are NULL by design (see 0011), and `WHERE` dropped it. One blocked keyword hid 278 of 339 posts, silently. Fixed with `COALESCE(col, '')`.
2. **Cluster naming misnamed 86% of the vocabulary.** See 0018.
3. **Natural-language feedback under-read hard refusals.** "I never want to see anything about crypto" produced a down-rank rather than a block. `FEEDBACK_INTENT_SYSTEM` now states the triggers as concretely as the cautions, and a refusal is recorded as both a block and a less-topic.

Also fixed on review: blocked keywords are LIKE-escaped, since a `%` in a keyword emptied both the feed and the write queue while `_` over-matched.
