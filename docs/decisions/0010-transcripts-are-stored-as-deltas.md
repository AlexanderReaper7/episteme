# 0010. Tool-loop transcripts are stored as deltas, not as growing snapshots

- Date: 2026-07-17
- Status: accepted
- Rule: a conversation's rows store what was added, chained by `chain_id` and `seq`. Reconstruction happens on read.

## Context

Every gateway call is persisted to `llm_calls`. A tool loop sends the entire conversation on every turn, so storing each call's request verbatim stored the whole transcript N times for an N-turn loop. The provenance page then deduplicated at render time to make it readable.

That render-time deduplication is the smell: the consumer was compensating for how the producer stored data.

## Decision

Rows store message **deltas**, chained by `chain_id` and `seq`. The full transcript is the concatenation of `request.messages` plus `response` in `seq` order. A prefix hash detects rewritten history and starts a fresh chain rather than storing a delta against a prefix that no longer exists. Tool loops opt in by wrapping themselves in `llm_conversation()`. Reconstruction lives in `web/admin.py:_group_calls`.

## Rejected

Deduplicating on render, which is what existed. It works, it is invisible in the data, and it means the stored bytes grow quadratically in turns while the page quietly hides it.

## Consequences

This is the origin of the standing principle "fix root causes, not symptoms", which now lives in CLAUDE.md. It is also an instance of the related one: store the canonical minimum and derive the rest, since a transcript is exactly derivable from its deltas.
