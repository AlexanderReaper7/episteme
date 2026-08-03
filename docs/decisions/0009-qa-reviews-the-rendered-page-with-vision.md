# 0009. QA reviews the rendered page, with vision, and may demote

- Date: 2026-07-17 (Phase 2.5)
- Status: accepted, amended by 0026 (how it edits) and 0028 (when it screenshots)
- Rule: QA judges the post a reader would actually see, not the JSON that produced it.

## Context

A draft can be valid JSON, pass every schema check, and still render as a broken page: a collapsed figure, a duplicated section, a caption with no image. Nothing upstream of the browser can see that.

## Decision

`worker/qa.py` renders each unscored post through the real web app (`web_internal_url`), screenshots it with headless Chromium (Playwright ships in the image), and puts that image in front of the main model along with a digest of the trusted sources. The model critiques, edits, and sets `quality_score`. It may demote a post to an aggregate card.

Screenshots are stripped before `llm_calls` persistence, so the provenance log stays readable and small.

## Vision, and a trap worth remembering

Vision has been enabled since ~2026-07-19: `mmproj` plus `image-min-tokens = 1024` on the Qwopus section of `models-preset.ini`. Re-confirmed by direct probe on 2026-08-02: a two-colour PNG came back "Red Blue", correct and in the order asked for, and the 136-byte image cost 1072 prompt tokens, so it really did expand to about 1024 image tokens.

**Qwopus is a reasoner.** A 60-token cap returned empty content with the entire budget spent on `reasoning_content`. Give a vision call room before concluding that vision is broken.

## Consequences

Real defects caught, from the transcripts: a black-hole mass stated as 1M instead of 4M solar masses, and a wrong year for an astronomical midpoint, both then confirmed fixed on re-review. Over 2026-07-18 to 08-01 the stage made 134 calls and scored 81 of 82 feature posts.

QA skips aggregate cards, which have no body to review.
