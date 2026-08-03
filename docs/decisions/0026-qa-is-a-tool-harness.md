# 0026. QA edits by section index through a tool harness, and gets no network tools

- Date: 2026-08-02
- Status: accepted (supersedes the editing mechanism of 0009)
- Rule: QA's index space is the body only. Its grounding set is fixed at write time.

## Context

Three findings of a code-review pass turned out to be one root cause: QA could only **replace the whole body**, and its only input was a screenshot.

So a fix to one prose paragraph obliged the model to re-emit a quiz whose `answer_index` is an attribute and whose `explanation` is `hidden`, neither of which appears in a screenshot. It had to guess, and could silently flip a correct answer to a wrong one. The prompt meanwhile told it to "judge whether the marked answer is the correct one" using information it was never given.

## Decision

**One shared driver.** `agent.run_tool_loop` turns the model, dispatches tools, enforces step and wall-clock budgets, and stops on a terminal tool. The writer's research loop and QA's editing loop are two tool tables over it, so the nudge, budget-refusal and stuck-loop logic exists once. `ToolReply` carries `stop`, `refused` and `follow_up`. `follow_up` exists because a `role: "tool"` result is a plain string while QA's `rerender` produces an image, which has to ride back as its own user message.

Writer behaviour is unchanged except that `demote_story` now finishes its turn before stopping, where it used to return mid-turn, which only makes the transcript coherent.

**QA's tools:** `replace_section`, `insert_section`, `delete_section` (all by index), `set_meta`, `rerender`, `finish_review`. Budgets are `qa_max_steps`, `qa_max_edits`, `qa_max_screenshots` and `qa_wall_clock_seconds`, replacing `qa_max_rounds`.

**Scoped hard.** The index space is the **body only**: the DB-built citation tail is neither listed nor addressable, and the `Section` union has no citation member to forge (see 0007). Image and video pass the writer's closed-set sanitizer (see 0013).

**No network tools, decided with the user.** QA's grounding set is fixed at write time, and a reviewer that can fetch reopens the prompt-injection surface for nothing.

**Every mutation returns the renumbered listing**, because an insert or a delete shifts every index after it.

**Rejections are tool results**, not failures. A bad shape, a bad index, non-candidate media, or removing the last quiz comes back as a tool error the model repairs in-conversation, and costs no edit budget. Previously one malformed section failed the entire `QAReview` and burned a repair retry re-emitting an otherwise-correct body.

**Tool arguments are grammar-constrained too.** The `section` parameter carries the real `Section` union (`schemas.section_param_schema`) with its `$defs` hoisted to the tool's `parameters` object, which is the document root llama.cpp resolves `#/$defs/…` against. Pydantic still validates afterwards, the same double enforcement used everywhere else at this boundary.

## The bug that made the verdict shape matter

`QAReview` is now a verdict only: `verdict`, `quality_score`, `critique`. It used to carry a replacement body, and `_require_quiz` fired on any non-None `revised_sections` regardless of verdict. So a `demote` that also carried a body failed validation, `request_validated` returned None, and **the demotion was discarded**, leaving the post unscored and re-reviewed forever.

Edits are flushed before every re-render and once when the loop ends, so they stand whatever verdict arrives, or does not.

## A defect introduced by this design, found on review the same day

`visual` was decided before the editing loop from what the **writer** left, so a chart or diagram **QA itself inserted** got no screenshot and no `rerender` tool, and then a score, which means never re-reviewed. That is post 427's blank box (see
0027) reintroduced through the editing path.

`run_tool_loop` re-reads its `tools` list every turn, so the table now grows in place (`_offer_rerender`, on the editor's own copy, leaving the module constants clean) and the tool result tells the model. It re-prefills the prompt, since tools render ahead of the messages, which is the accepted trade for a rare edit. The backstop for "added with no budget left" is that `quality_score` stays NULL, which returns the post to the qa queue where `visual` reads true from the stored sections.

## Risk, stated plainly

Not verified live. Unit and regression tests only (`test_qa.py`). Weigh that against what it replaced: the old whole-body stage was **not** an unverified path. It ran 134 calls across 2026-07-18 to 08-01 and scored 81 of 82 feature posts, and its transcripts show real work. So this is a rewrite of a working path, not a first implementation of a dead one. The defects it fixes are real, but the regression risk is correspondingly higher and the next pipeline run should be watched rather than assumed.

## Open, pre-existing

5 of those 134 calls failed with a bare `400 Bad Request` from llama-server: 1 on 07-18, 1 on 07-19, 2 on 07-31, 1 on 08-01. Cause unknown. Stored request sizes are post-`_strip_images`, so they say nothing about the real payload. Worth re-checking after this lands, because the new first turn is **larger**, a source digest plus full body JSON plus a screenshot where it used to be digest plus screenshot. Partly addressed by 0028, since most reviews no longer carry a screenshot at all.
