# 0051. The window is the measure

- Date: 2026-08-30
- Status: live-verified 2026-08-30 at 2560px, 700px and 390px
- Rule: every page's content takes the window. `--content-width` is `100%`, `main`'s padding is the only horizontal gutter, and no surface centers itself inside a typographic measure - the article page included. (A feed card's image was a share of the window here; 0052 made it a third of the card.)

## Context

`--feed-width` was 58rem, a centered reading measure, and `.article-page` was 46rem. On the 2560px viewport this app is actually used on, that left 64% of the screen empty on either side. The token's own comment argued for it, and the argument is the standard one: continuous prose is read fastest at 60 to 90 characters a line.

The reader asked for the full width anyway, article page included, having been shown what it costs: a card summary goes from ~97 characters a line to ~270, which is roughly three times the readable maximum. **That trade is the decision here, and it was made with the number in hand.** A multi-column card grid was offered as the alternative that spends the width on more cards rather than longer lines, and turned down.

So this document is not "wide is better". It is: the measure was a default nobody chose, the reader chose otherwise for this screen, and the code now says which.

## What the token means now

`--content-width: 100%` is read by `.feed`, `.article-page`, `.admin-shell`, `.tune-page`, `.glance`, `.correspondent-page` and `.error-page`. It is `--content-width` and not `--feed-width` because it stopped being the feed's alone the moment the article page and the admin shell read it.

Three literals died with it: the article page's 46rem, the admin shell's 90rem, and Matsedel's `max-width: 96rem`. That last one is the interesting one - it existed only to *escape* core's 58rem, because four kitchen columns wrapped inside it (0046). Left in place it would have become a ceiling, and the plugin would have been the one page in the app that did not honour the change.

`main`'s horizontal padding went 1rem to 2rem. It was never what you saw when content stopped at 58rem; against the window it is the entire margin.

Small `ch`-capped blocks inside admin panels (`.stage-intro` at 70ch, `.stage-note` at 82ch) are left alone. Those are one-sentence explanations bound to a control, not page containers, and a hint that runs 270 characters is not a hint.

## The image is a share of the window

**Superseded 2026-08-31 by [0052](0052-a-card-is-two-thirds-text-one-third-picture.md).** The share is of the CARD, not the window, it is a fixed third with a 16:9 floor, and both the `vw` clamp and its `max-height` twin are gone. The rest of this document stands. The measurement below is kept because it is why `vw` and not a percentage, which 0052 does not repeat.

A 9rem thumbnail sized for a 928px column reads as an afterthought on one nearly three times as wide, so the banner scales with the window: `width: clamp(9rem, 16vw, 24rem)`.

**`vw` and not a percentage**, which is the part that cost a round of measurement. The banner sits in an `auto` grid track, an `auto` track sizes to what is in it, and a percentage width has nothing definite to resolve against inside one. Measured at `20%`: the track took **1294px of a 2481px card while the image in it drew 259px**, so the summary wrapped at 1150px and the right half of every card was empty. `vw` is definite, the track sizes to the image, and it still collapses to zero on a card that has none.

The height is `align-self: stretch` between a floor and a ceiling, not an `aspect-ratio`. Any fixed ratio at this width makes the picture taller than the three or four lines of summary beside it, and the card is sized by its picture again - which is what 0050 removed the full-width banner to stop. Stretching alone is worse in the other direction: measured at 700px, a card whose summary wrapped to 14 lines pulled a 144px-wide image to **474px tall**. So `min-height: 8rem` keeps a short summary from squeezing it to a strip, and `max-height: clamp(9rem, 16vw, 24rem)` - the same expression as the width - keeps it from ever being taller than it is wide. Under 40rem all three collapse to one value and the image is an 88px square.

## Consequences

- **The expand control effectively disappears at 4K.** Nine lines at ~270 characters is ~2400 characters and the longest summary in the corpus is 1188, so nothing clamps at 2560px (measured: 0 of 20). It is not dead code - at 700px, 15 of 20 cards clamp and carry the control. The clamp is now a narrow-window feature, and the nine-line number in 0050 was measured against a 930px column that no longer exists at this screen size.
- The feed shows about 5 cards per viewport at 2560px, against 4 before, and the images went from 144px squares to 384px wide.
- The admin dashboard's grid now makes 5 tracks of 438px. The LLM endpoint card sits alone on the first row because the card after it spans `1 / -1` and forces a break. That is a pre-existing artifact of DOM order that three tracks hid and five do not; the fix is a card-ordering decision on the dashboard, not a width one, and it is left open.
