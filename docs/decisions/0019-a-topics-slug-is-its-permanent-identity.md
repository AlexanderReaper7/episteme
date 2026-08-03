# 0019. A topic's slug is assigned once and never moves

- Date: 2026-07-30
- Status: accepted
- Rule: `rename` changes the label only. `merge` is the one operation that ends an identity, and it repoints every reference.

## Context

Weights were keyed by `slugify(label)` while `rename` moved the label and not the slug. So a rename discarded the topic's learned weight and left the row unreachable by `_by_slug` under its own new label, which meant the next emission created a duplicate row.

## Decision, chosen with the user

`slug` is assigned once and never moves. `rename` changes `label` only and adds `slugify(new_label)` to `aliases`, so the new wording resolves back to the same row. `merge`, which genuinely ends an identity, repoints `feedback.topic`, `topics_snapshot` and `parsed_intent[].topic` at the survivor, and the API route rebuilds and rescores afterwards.

Feedback rows store **slugs** (`topics.resolve_slugs`). `Candidate.topic_slugs` replaced `Candidate.topics`, resolved through one `topics.slug_index` per pass. `profile.replay` keeps `slugify` as an idempotent **normalizer**, since a slug slugifies to itself, which is also why no data migration was needed: rows written before this land on exactly the key they always did.

## Why, beyond fixing the bug

Forward compatibility with a related-labels graph, where disliking "blue" also weakly dislikes "color" and "ocean". Edges need stable nodes. A key derived from the current label re-keys the node and dangles every edge on the first spelling correction.

## Consequences

`feedback.keyword` is deliberately not the `topic` column. A keyword is matched literally against title and summary; a topic is slugified into the vocabulary. One mix-up there turns a down-rank into content removal.
