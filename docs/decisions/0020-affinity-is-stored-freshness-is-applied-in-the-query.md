# 0020. Affinity is stored on the post; freshness is applied in the feed query

- Date: 2026-07-29 (Phase 4)
- Status: accepted
- Rule: ranking never rewrites rows as time passes.

## Context

A feed ordered by "score plus recency" can be computed two ways: bake time into the stored score and rewrite every row as it ages, or store the time-independent part and apply age at read time. The first is a background job that touches the whole corpus continuously and is wrong the moment it finishes.

## Decision

Seven `Scorer` implementations behind a registry. `Post.affinity_score` is stored with a `score_components` breakdown, so the number is explicable rather than just present.

The feed orders by `tanh(affinity/scale) + age_in_tau_units`, computed in SQL against a **fixed epoch**.

Three properties, all deliberate:

- **bounded**, so a soft signal reorders rather than buries. The measured bound held: 668 rank inversions on 339 posts, maximum 19.3 hours, inside the 2·τ = 72h the formula guarantees.
- **symmetric**, so a dislike and a like of equal magnitude move a post equally far in opposite directions.
- **pure per row**, so keyset pagination stays sound. Infinite scroll depends on it: a rank that referred to other rows would shift under the cursor.

A NULL `affinity_score` reads as neutral, which is what makes the feed sane before the vocabulary is bootstrapped at all.

## Consequences

The keyset cursor is taken from the SQL `rank` column rather than recomputed with Python's `math.tanh`, so the comparison never spans two libm implementations.

Write-side uses the same numbers: the write queue is ordered by triage quality plus a bounded affinity term, and triage and the writer receive a qualitative reader digest.

## Deferred by decision

Implicit dwell and scroll signals, the "why am I seeing this" chip, diversity and serendipity quotas, and OpenAlex authority.
