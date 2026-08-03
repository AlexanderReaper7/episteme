# 0007. Citations are built from the database, never from LLM free text

- Date: 2026-07-17, extended 2026-07-18
- Status: accepted
- Rule: `sources` and `further_reading` sections are constructed in code. No stage may write a citation, and QA cannot address one.

## Context

A generated article that cites a paper which does not exist is worse than one that cites nothing, because it looks like provenance. A language model asked to emit a citation list will produce plausible URLs whether or not it saw them.

## Decision

The `sources` section is built from the story's `Item` rows. The `further_reading` section is the writer's `further_reading_urls` selection **intersected with the fetch log**.

That intersection is a closed set, and the split of authority is the point: the model contributes judgment about which fetched pages were actually relevant, so dead-end fetches stay out, but only membership in the fetch log can put a URL on the page. The log stores final post-redirect URLs, so what is cited is what was actually reached.

The section index space QA can address excludes both (see 0026), and the `Section` union has no citation member to forge.

## Rejected

Letting the writer emit the citation list and validating it afterwards. Validation would have to resolve every URL, which is a fetch we are not entitled to make for that purpose, and a URL that resolves is still not a URL the writer read.

## Consequences

Every published citation is traceable to a row or a logged fetch. Verified live on 2026-07-17: the DB-built `sources` section matched the DB rows exactly on all ten articles of the first full run, including a three-source cluster.
