# 0045. The long-form post kind is called `article`, stored value included

- Date: 2026-08-28
- Status: accepted
- Rule: `Post.kind` is `article` or `aggregate`. The word "feature" no longer names a post kind anywhere in live code, live docs or the database. Records that are frozen by policy keep the old word.

## Context

`feature` had two meanings in one repository. `config.py` used both, 234 lines apart: `llm_host_agent_url: str = ""` documented as "empty = feature off" at line 113, and the post kind at line 347. `style.css:31` declared `--card-feature-border` and then explained it in the comment as the "generated-article card accent border", which is the collision written out in full by whoever wrote the line.

The prose had already picked a side. Counted before the change, the repository held 163 occurrences of "article" against 140 of "feature", and the ones that mattered were load-bearing: the chat tool is `write_article_from_url`, the writer prompt says "write an article", `research.py` talks about article text. Only the enum value, the CSS class and a scattering of comments still said feature. `article_id` appeared zero times, so the name was free.

## The stored value changes too

The alternative was to rename in code and leave `posts.kind = 'feature'` in the database, mapped at the boundary. That is the version of this change that keeps a translation layer alive forever, and every future reader has to learn both words anyway to understand the mapping. The rename is worth doing exactly once, at the bottom.

Migration `192537060e8f` is one `UPDATE posts SET kind = 'article' WHERE kind = 'feature'`. It is safe to make it data-only because `posts.kind` is a plain `String(20)`: no Postgres enum type, no check constraint, and no server default since `3daf6dbaefc3` dropped the last one. 173 rows carried `feature`, 473 carried `aggregate`, and the WHERE clause leaves the second set alone.

## What was not renamed, and why

Two classes of file keep the old word on purpose.

**Applied migrations.** `2c4a5f0070bf` matches `p.kind = 'feature'` in a backfill and `3daf6dbaefc3` names it in a comment. Both already ran. They record what happened against the schema of their day, and editing them would make the history disagree with itself while changing nothing that executes.

**Decision records written before today.** 0008, 0009, 0011, 0025, 0026, 0035 and 0040 all use "feature" for the post kind. The decision log's own README says it: nothing there is edited to reflect later changes. `GLOSSARY.md` carries the mapping instead, under `### Article`, so a reader who meets the old word in an old record can find out what it was.

The ordinary English word survives everywhere it was always the ordinary English word: the governor's feature detection, the benchmark charts, `pending.py`'s "feature flag" comment, 19 occurrences in all. Each was checked against one criterion, rename if and only if the occurrence denotes the post kind, rather than decided one at a time.

## Rejected

- **Keep `feature`.** The word is unavoidable in its software sense in a codebase with feature flags and a resource governor, so one of the two meanings had to move, and the post kind is the one with a better word available.
- **`story`, `piece`, `writeup`.** `Story` is already taken by the cluster, and the other two are not what the rest of the code and the UI already call it.
- **Alias the old value on read.** A compatibility shim for a single-user database with 646 post rows, to avoid one UPDATE.

## Consequences

- 21 files renamed wholesale, 3 line-targeted where the same file used both senses. `uv run pytest -q` passes 827 tests and `ruff check src tests` is clean after it.
- A grep for `feature` in `src/` and `tests/` now returns only the ordinary word. That is the check that this stayed done.
- Old provenance pages and old `llm_calls` transcripts still contain the word in prompt text. Nothing reads it as a value.
