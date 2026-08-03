# 0025. Every feature carries a quiz, choices are shuffled per read, and the reader is never scored

- Date: 2026-08-02
- Status: accepted
- Rule: the quiz requirement is enforced in code, not in the prompt. No tally, ever.

## Context

From TODO "improve quizes and add more questions", with two requirements added by the user mid-work: quizzes are mandatory, and choice order must be randomised.

No migration was needed. `sections` is JSONB, so the shape change needed no DDL, and the 16 pre-existing posts were rewritten in place with one SQL statement. The database is disposable before v1, the user's call: "do what is simplest".

## Decision

**Shape.** `QuizSection.questions: list[QuizQuestion]`, 1 to 5, replaces the flat `question` / `choices` / `answer_index` / `explanation`. One canonical storage shape, so nothing downstream carries a compatibility branch. `_SECTION_TEXT_FIELDS` became `("questions",)`; `_count_words` recurses and ints contribute 0.

**Mandatory.** This has to live in code: the JSON-Schema grammar constrains each section's *shape* but cannot demand a member in a list. The writer's whole draft is checked by `schemas._require_quiz` in a `PostDraft` validator, and `agent.request_validated`'s repair-retry loop puts the failure straight back in front of the model.

QA enforces it **somewhere else on purpose**: it edits one section at a time, so the check sits on the action that could break it. A delete or replace leaving no quiz is refused as a tool error. Prompts match: the writer's says "REQUIRED, not your call"; QA's says to drop the bad *question*, never the check.

**Randomised per read, not per write**, at the user's explicit instruction after I built it the other way. It was originally a Jinja filter, and that filter was silently dead in production: `_post_page_etag` hashes the canonical `post.sections`, so a re-read got a 304 or a `stale-while-revalidate` hit and the identical order, and the ETag was naming two different bodies (see 0031).

Shuffling moved into `app.js:shuffleChoices`, which permutes the buttons in the DOM per page **view**, surviving caching and reloads. Each button keeps the `data-index` it was rendered with and `data-answer` stays the **stored** index, so the marked answer follows its text through any permutation with nothing recomputed. The served HTML is deterministic again, which is regression-tested.

**Never scored.** A "N of M correct" tally was built and then removed on the user's instruction. Each question resolves on its own; the check reports what landed and never grades the reader. Spec §1, zero manipulative mechanics. `test_quiz_never_scores_the_reader` exists so it cannot creep back.

## Consequences

The QA screenshot shows an ordering no reader will ever see, which is irrelevant because QA reads the canonical JSON. The QA prompt still forbids referring to a choice by position, and the writer prompt still forbids "both of the above"-style choices.

Rendering: one "Check your understanding" heading per section, a `.quiz-item` per question with its own `data-answer`, and "Question N of M" only when there is more than one. JavaScript state moved from the section to the item, so answering one question no longer freezes the rest.

## Verified live, 2026-08-02, in the browser

Choice order and `data-answer` both moved across 8 renders of post 422 while the marked answer tracked its text every time. A temporarily-2-question post answered wrong then right showed correct and wrong colouring, per-question explanation reveal, and siblings staying live. The post was restored byte-identical afterwards.

Not verified live: an actual writer run producing a multi-question quiz, since there has been no pipeline run since.
