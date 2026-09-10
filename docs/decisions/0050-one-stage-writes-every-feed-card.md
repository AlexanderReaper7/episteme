# 0050. One stage writes every feed card, and a post is not in the feed until it has one

- Date: 2026-08-30
- Status: live-verified 2026-08-30, backfill included (683 posts, 78 minutes)
- Rule: `posts.summary` is the whole text of a feed card, and the `summarize` stage is its only author for every kind whose `PostKind.summarized` is true. It runs after `qa`, so an article is summarized from the body the reader would actually open. A replaced summary moves to `post_summaries`; a stale one is marked by clearing `summarized_at`, never by clearing the text. The feed shows no post without a summary.

## Context

The reader asked for a feed whose cards stand on their own: scroll the stream, get the substance, click only for depth. What the feed did instead, measured on 2026-08-30:

| kind | published | what the card said |
|---|---|---|
| aggregate | 503 | `primary.extracted_text or raw_content\|striptags`, cut at 240 chars |
| article | 180 | `posts.summary`, cut at 300 chars |
| filed | 5 | the correspondent's own summary |

73% of the feed was a raw RSS blurb sliced mid-word. No model ever wrote a sentence for an aggregate card: `ensure_aggregate_post` mints an identity-only row and the template renders the item at read time.

The other 27% was not much better *by specification*. `PostDraft.summary` was described as **"One-paragraph hook"**, capped at 1000 characters, and then truncated to 300 by the template. A hook is built not to stand alone, and it was guillotined anyway. `.card-snippet` was styled `var(--muted)`: a caption under the title, not the content.

Three problems, three causes, none of them in the template.

## The condense pass had to go first

Before the writer saw anything, `_condensed_source` ran every source item over `summarize_above_chars` (2500) through the fast model and handed the writer 3-5 sentences instead of the article. So the writer's whole view of a 6000-char ScienceDaily release was a summary of it, plus its own fetch tools.

Measured over the 183 stories that produced an article:

```
avg items/story   1.1        max 6
source chars      median 4210    p90 9704    max 26218
```

It was condensing about half of all written stories to save ~2,400 tokens at p90, against a 65,536-token window and a writer prompt already near 19k. It paid for those tokens with the most trustworthy text in the system, and then the writer spent a polite HTTP round trip re-fetching pages whose text was already in `source_items`.

Removed. `_writer_sources` hands over the whole text under two caps (40k per item, 80k per story) that exist so one pathological 128k-char Nature item cannot eat the window. Nothing in the corpus reaches either.

## The summary is written last, by one author

The card is now written by a `summarize` stage on the `fast` role, placed after `qa` in `run_pipeline`. Three things follow, and each removes a way for a card to disagree with its post:

- **The writer is not asked for a summary.** `PostDraft.summary` is gone. A hook written before the article existed, then overwritten by a summary of what the article turned out to say, was generation spent twice to arrive at one answer.
- **QA cannot edit one.** `set_meta` became `set_title`. QA runs before the summary exists; an editor rewriting it would be writing an input to a stage that has not run.
- **Two prompts, one output.** `ARTICLE_SUMMARY_SYSTEM` reads the finished body; `AGGREGATE_SUMMARY_SYSTEM` reads every outlet in the cluster. Not one prompt with a mode flag, because the failure each has to prevent is different: an article summary drifts into describing the post, an aggregate summary drifts into saying the headline again in longer words.

`filed` declares `summarized=False`. A correspondent's summary is the correspondent's words (0046), and rewriting it would be Episteme claiming to have read the source.

## `max_length` is a guillotine, not a target

Found by running it, and it would not have been found any other way. `CardSummary.summary` first carried `max_length=800` as a length control. llama.cpp compiles that into the grammar, so the string stops at exactly 800 characters with the model mid-word, and pydantic then validates 800 <= 800 and stores it. No retry fires, because nothing failed. The first three real summaries came back at 800, 799 and 795 characters, two of them cut mid-word.

Two changes, and neither is "lower the cap":

- `max_length` is 1200 now and is a runaway guard. Nothing is allowed to depend on it for length.
- `_reads_as_finished` rejects a summary that does not end on punctuation, which turns a silent guillotine into a `complete_json` repair retry. It catches the case wherever it comes from, including a model that simply stops.

The length the card wants is asked for in the prompt, as a **sentence count**. A character budget was tried first and did not bind at all: told to aim for 400-500 characters and never exceed 600, the model returned 743, 750 and 815.

## Staleness is a column, not a convention

`summarize_pending()` is due when the summary is missing, when `summarized_at` is NULL, or when `stories.last_item_at` is newer than it. The third clause is what earns the column: a cluster that absorbs another outlet moves past the summary describing it and **invalidates its own card**, with no code anywhere remembering to.

The one thing no column shows is a QA body edit, so `qa._flush` clears `summarized_at` at the single choke point every body edit already passes through. It clears the stamp and **never the text**, which is what keeps a published post from ever having no card: the previous true summary stands until a better one exists.

## History

A replaced summary is inserted into `post_summaries` before the new one lands. Only superseded versions live there, so `posts.summary` stays the single current value and there is no "which row is live" question to get wrong.

`llm_calls` already holds every summarize call's response scoped to the post (0012), so this is technically a second copy. It is kept anyway because `llm_calls` is pruned on the retention tiers, and history with an expiry date is not history. The table carries no `model_used`: nothing records which model wrote a summary that is *already stored*, so the column could only be NULL or a guess at the model doing the replacing.

## The feed requires a summary

The summary is written after the post is published, so between `write` and `summarize` an article exists with no card text. It waits out of the feed rather than appearing as a bare headline — one clause in `web/app.py:_visible_now()`, beside `publish_at` and `expires_at`.

This is not a filter compensating for bad production. `_visible_now` is where "in the stream" is defined, and "finished enough to show" is the same kind of condition as "not expired yet". The alternative considered was a third `status` value (`draft`), which is the more literal reading but would have required reviewing all twelve `status == "published"` query sites, where published currently means visible.

## Rendering

The card renders the summary whole. `truncate(300)` and `truncate(240)` are gone: a summary written to stand alone and then cut mid-word is the one outcome the whole change exists to prevent.

`.card-snippet` clamps at nine lines with a fade. Nine is measured, not derived. Four earlier numbers were arithmetic and all four were wrong, including two written in this document before the backfill existed to check them against, which is the argument for looking. What the corpus of 688 summaries actually says: median 695 characters, p90 1009, max 1188. What the browser says at 2560px: the text column beside the thumbnail runs about 97 characters, so the nine-line boundary sits between a summary of 926 that fits and one of 1092 that does not - call it 930. The two agree, which is the point of taking both: 17% of summaries are over 930 characters by `length()`, and 16% of 80 cards scrolled in a browser carried the clamp. The colour moved off `var(--muted)` toward `--text`: it is the content now.

**Every number in this section was measured against a 930px column, and 0051 took that column away.** At the full window a line runs ~270 characters, so nine lines is ~2400 and nothing in the corpus clamps at 2560px. The clamp and the expand control are a narrow-window feature now: at 700px, 15 of 20 cards still carry one.

The banner became a thumbnail. A 16:9 image across the top of the card was ~400px of picture above ~120px of text, and 1.5 cards fit a 2560x1400 viewport - the opposite of scrolling a stream. `.card` is a two-column grid now, 9rem square image on the right, text on the left, shrinking to 5.5rem under 40rem of width. Measured on the same screen afterwards: card height 620px down to 318-389px, four cards in the viewport.

What the clamp cuts is one click away, in the feed. `_card_summary.html` renders a **Show more** button under every card summary, hidden until `markClampedText` has measured that the text overflows - hidden by default and revealed by the measurement, because the other way round flashes the control onto every card and takes it off most of them a frame later. It is a `<button type="button">` and not an anchor, since every anchor on the page is boosted and a boosted `href="#"` swaps `#main-content` out from under the feed. It takes the same `position: relative; z-index: 2` lift as `.card-provenance`, or the stretched `.card-link` swallows the click and navigates instead.

The control matters most where the reader cannot get the text any other way. An article card clamped at nine lines still has the article behind it; an **aggregate** card links straight out to the source, so before this the clamped tail was simply unreadable. About one card in six clamps (13 of 80 scrolled), which is the size that settles the design: too many to leave unreadable, too few to justify making every other card taller by raising the clamp.

`.is-clamped` and `.is-expanded` are separate classes on purpose. The first records that the text overflows, which is a fact about the text; the second records that the reader opened it. Keeping them apart is what keeps the button on screen once the card is open: `markClampedText` skips an expanded snippet, because a snippet with no clamp measures as fitting and would take its own control away.

The article page lost its standfirst. `post.html` rendered `post.summary` under the title, which read as a lede while the summary was a hook; now it is 3-5 sentences saying what the piece found, so a reader arriving from the card would read it and then read the article saying the same thing. The card is where that paragraph belongs.

The fade applies only to `.card-snippet.is-clamped`. No CSS selector can ask whether an element overflowed, and a mask on every snippet greys out the last line of every summary that fit — which is most of them. `app.js:markClampedText` compares `scrollHeight` against `clientHeight` on every `htmx:load`, on resize, and once when `document.fonts.ready` resolves.

## Rejected

- **Cleaning the raw blurb** (sentence-boundary trim, boilerplate strip) instead of generating. Cheapest, and a symptom fix: 27% of items in aggregate stories carry under 600 characters, and a blurb with no substance stays a card with no substance.
- **Extending `TriageResult` with a summary field**, which would have cost zero extra LLM calls since triage already runs on every story. Rejected because triage sees only 300 characters per item (`_story_digest`) — the same truncation the card was already showing — and raising that budget to write a summary makes triage pay for it on every story, including the ones it skips.
- **Keeping the writer's summary as a fallback** when the fast model is down. Two authors for one field, and the fallback is a hook nobody wanted to read.
- **Splitting the stage in two**, summarizing aggregates right after `triage` while `fast` is already resident. It would shrink an aggregate card's wait from hours to seconds — but with the feed gate above, waiting is invisible: the card arrives finished instead of arriving empty.

## Consequences

- Applying the migration removed 503 aggregate cards from the feed until the stage reached them. The 180 articles kept their old hooks and stayed visible, replaced in place. The backfill (`POST /api/jobs/defer/summarize`) was 683 fast-model calls and ran in 78 minutes, about 8.8 posts a minute.
- One model swap is added to the nightly run: `fast` (triage) -> `main` (write, qa) -> `fast` (summarize).
- `summarize_above_chars` is gone from settings, along with `SourceSummary` and `SUMMARIZE_SYSTEM`.
