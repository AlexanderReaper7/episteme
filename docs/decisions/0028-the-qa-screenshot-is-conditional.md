# 0028. QA screenshots only posts containing a chart or a diagram

- Date: 2026-08-02
- Status: accepted
- Rule: `qa.needs_render` decides. Chromium launches lazily.

## Context

The user asked: "is the screenshot needed? my thought was it would review any custom section that isn't just formed from the template".

That is the right question, and the answer is that such sections exist, there are exactly two of them, and one was broken (see 0027).

## Decision

Seven of the nine section types render through a Jinja branch that is a **total function of their JSON**, so the canonical listing already says exactly what the reader sees. Post 454's transcript shows the model working that way by preference, quoting `answer_index` and judging distractors off the JSON.

`chart` and `diagram` are different: a Vega-Lite spec and a Mermaid source are programs, and whether they draw anything is not decidable from the text. Those keep the screenshot, and `rerender` is only in the tool table for those reviews.

Chromium now launches **lazily**. Before, one browser was launched per batch inside the `try` that turns any failure into "QA stage unavailable", so a missing Playwright skipped even the posts that needed no browser.

## Measured

Corpus impact at the time: 1 of 89 posts has a chart, 0 have diagrams.

## Consequences

Almost every review is now text-only, which also shrinks the first turn that 0026 had just made larger, and is therefore the partial mitigation for the unexplained `400 Bad Request` responses noted there.

A chart or diagram QA *inserts* must still get a screenshot. That path was initially missed and is handled by growing the tool table in place; see 0026.
