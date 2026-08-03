# 0018. The topic vocabulary is canonical and resolved in code, bootstrapped in two phases

- Date: 2026-07-29, applied live 2026-07-30
- Status: accepted
- Rule: whatever a model emits as a topic is *resolved* against the `topics` table. The prompt is never trusted to produce a canonical label.

## Context

Weights have to key against something stable. A model asked for topics will emit "quantum physics", "quantum mechanics" and "quantum" for the same field, and a profile keyed on free text learns three unrelated preferences.

## Decision

A `topics` table plus `recommend/topics.py`. Every emitted label is resolved to a row in code: exact match, alias match, then embedding similarity above `topic_bootstrap_threshold`, then a new row if nothing matches.

Bootstrapping from the existing free-text tags is **two-phase on purpose**. `propose_topics` computes a proposal and writes it; `/admin/topics` renders it for review; `apply_topics` commits it and rewrites every story and post topic array. Nothing is applied by the job that computes it.

`resolve_entries` (raw label to row) is the primitive. `resolve` and `resolve_slugs` are views of it. That ordering matters: `resolve` dedups and drops, so it is **not positionally aligned with its input**, and `record_nl` originally zipped against it. "less crypto, more quantum computing" could be recorded as its own inverse, into the canonical `parsed_intent`.

## The naming defect, 2026-07-29

Asked to name 734 clusters in one call, the fast model held index alignment for 98, slipped one, and never recovered. The {quantum physics, quantum mechanics} cluster came back named "roman history". A wrong index is a well-formed response, so nothing caught it.

`_name_clusters` now sends only **multi-member** clusters, because a singleton's canonical name is its one member, and the singletons were 656 of the 734 that made the listing unmanageable. It batches by `NAMING_BATCH` and rejects any name sharing no word with its cluster (`_plausible_name`), falling back to the most frequent member.

## The live run, 2026-07-30

860 raw labels became 749 canonical topics: 82 multi-member clusters, 667 singletons. Applied after a pre-apply `pg_dump`. Every story and post topic array rewritten, zero raw variants left in the database or rendered in the feed. `quantum-physics` correctly absorbed `quantum-mechanics` and `quantum`.

Naming cost fell from 1 call / 570s / 14 199 output tokens to 4 calls / 24.7s / 2050.

**The 0.86 threshold is right, not too strict.** An earlier guess that the weak collapse meant a bad threshold was wrong: the top singletons are `deep-sea biology`, `public health`, `archaeology`, `neuroscience`, `genetics`, `ecology`, `physics`, which are genuinely distinct fields rather than near-duplicates. The distribution is Zipfian, 484 of 749 used exactly once and the top 80 covering 65.6% of all tag uses, which is why `topic_vocabulary_prompt_limit=80` is adequate. Lowering the threshold would start fusing distinct fields.
