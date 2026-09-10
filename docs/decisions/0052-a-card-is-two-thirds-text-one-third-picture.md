# 0052. A card is two thirds text and one third picture, and the summary is clamped by the room it has

- Date: 2026-08-31
- Status: live-verified 2026-08-31 in Firefox at 2560x1400, 900x600 and 390x844
- Rule: a feed card's picture is a third of the card (`2fr 1fr`), never a width in `px`, `rem` or `vw`. When the window is taller than it is wide the picture moves above the text and takes the whole card. How much of the summary shows is a **measurement**, not a constant: `app.js:fitSnippet` gives the snippet as many whole lines as fit in the window after everything else in the card, and writes the number into `--card-snippet-lines`.

## Context

0051 put the picture beside the text at `clamp(9rem, 16vw, 24rem)` and refused it an aspect ratio, so that the card would be sized by its text and not by its image. Measured at 2560px the day after: **384px of a 2479px card, 15.5%**, a 245px card, and a summary of 815 characters set three lines deep across a 2081px column.

The reader looked at that and asked for a third of the width, expecting the taller card to give the summary more room. Both halves of that are worth writing down, because only one of them turned out to be true.

## Decision

**The fraction is the layout.** `.card:has(.card-banner)` is `minmax(0, 2fr) minmax(0, 1fr)`; a card with no picture is one track. `:has()` and not an `auto` track that collapses, because the `onerror` handler *removes* a dead banner from the DOM and the body has to reclaim the width when it does.

**The height comes from the width, in the card's own units.** `container-type: inline-size` on the card makes `cqi` mean "1% of this card", so `min-height: 18.75cqi` is 16:9 at a third of it, at every window size, with no breakpoints. It is a floor and not a height: a summary that runs longer than the picture is tall makes the card taller and the image stretches into it through `object-fit: cover`.

This reverses 0051's "not an `aspect-ratio`" with the reader's eyes on the result. The reason 0051 refused a ratio still holds and is now the accepted cost, below.

**Portrait stacks it.** `@media (orientation: portrait)` puts the picture in row 1 at `aspect-ratio: 16 / 9` across the full width and the body under it. The query is `orientation` and not a pixel breakpoint because the question the layout asks is the proportion of the space, which a width in pixels cannot answer: a narrow window on a wide monitor gets the same answer as a phone, and a phone in landscape gets the side-by-side layout it has room for.

**The clamp is measured per card.** Nine lines was a constant measured once, at 2560px, against a column that no longer exists; 0051 already recorded that it clamped 0 of 20 cards at that width and 15 of 20 at 700px. `fitSnippet` reads what the card has instead:

```
budget = window.innerHeight - header - 48px          (the next card's top edge stays visible)
taken  = body padding + every other child of the body
       + the summary block's non-text parts, the expand button reserved at one line
       + the picture, when it is ABOVE the body rather than beside it
lines  = floor((budget - taken) / line-height), never fewer than 3
```

Everything is measured from the DOM rather than derived from the stylesheet, because the two layouts and the three card kinds put different things in the body and an arithmetic copy of that here would be wrong the first time a card grew a row. The button is reserved at a line whether or not it is currently visible: it is `display: none` until the text is known to overflow, so on the pass that decides the count it measures zero, and without the reserve the card asks for one line more than it has.

`.card-body` is a flex column and `.card-summary` is the item that absorbs the slack, which is what makes the space measurable at all. It also puts the rating row on the card's bottom edge, level with the bottom of the picture.

## What it costs, measured

At 2560x1400 the picture is 812px of a 2479px card (32.8%) and 465px tall, the card is 494px, and the summary is **four lines of a possible 46**. The text column has ~250px it cannot fill: an 815-character summary is 4 lines at 1650px wide, and no clamp can make text longer. So the reader's second expectation did not hold - a third of the width buys a bigger picture, not more summary, on *this* screen. The feed shows 2.5 cards per viewport where 0051 showed 5.

The measurement earns its place at the other end. At 390x844 the picture is on top, the card is 712px against a 728px budget, and the summary shows 8 lines of ~24 with the fade and Show more. At 900x600 the card fills the window exactly.

## Rejected

- **Capping the summary's line length** (`.card-snippet { max-width: 90ch }`) to make the text use the height instead of the width. Measured at 2560: 90ch resolved to 737px of a 1650px body and the summary grew to 8 lines - and the emptiness moved from *under* the text to *beside* it, which reads as a broken layout rather than as padding. It also reverses 0051's central trade, which the reader made with the number in hand.
- **Capping the picture's height** (`min-height: min(18.75cqi, 15rem)`), which keeps ~300px cards and no slack at all at 2560px. It is one value away if the tall card wears badly; the reader asked for the tall card knowing what it costs.
- A pixel breakpoint instead of `orientation`. `max-width: 40rem` was what shrank the old thumbnail to a square, and it cannot tell a phone from a narrow window on a 4K monitor - which is exactly the difference that decides whether the picture fits beside the text.

## Consequences

- 0051's "The image is a share of the window" section is superseded: the share is of the card, the ratio is fixed, and the `vw` clamp and its `max-height` twin are gone. The rest of 0051 - full-width pages, the `--content-width` token - stands.
- 0050's nine lines is now the no-JS fallback and nothing else.
- `.card` is a container (`container-type: inline-size`), so any future `cq` unit inside a card resolves against the card. It already was the containing block for `.card-link`, so nothing moved.
- `tests/test_feed_cards.py` holds the layout properties: the third, the single track without a picture, the portrait stack, the variable clamp, and the button's reserve.
