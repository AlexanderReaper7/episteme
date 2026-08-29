# 0048. The browser never colours a link, and never marks one as visited

- Date: 2026-08-29
- Status: accepted
- Rule: every link's resting colour comes from `style.css` or from the component it sits in, never from the browser's own sheet. **No rule anywhere singles `:visited` out**, so a link looks the same whether or not it has been opened. Hovering any link changes something.

## Context

`style.css` had 3022 lines and not one rule for a bare `a`. Components set colours where someone had noticed: `.site-nav a`, `.attribution a`, `.section-prose a`. Everything else fell through to the browser's user-agent sheet, which on a dark scheme paints `:link` a blue nobody chose and `:visited` a purple nobody chose either.

Measured across twelve pages, 111 anchors: **13 were taking their colour from Chromium.** The week arrows and the stats link on `/c/matsedel`, the back link on its stats page, one link in `/admin/jobs`, the "See and edit what the feed has learned" line under an article, and every `.card-link` in the feed. That last one is invisible, an empty anchor stretched over a card, so it never showed. The rest did.

## Two colours, and one of them is a claim about the reader

The purple is the part worth writing down. The feed's whole job is to order things by freshness and interest (spec §8), and a second colour arriving from the user-agent sheet ranks them again underneath that, by "already clicked". Nothing in this app decided to say that. A single-user feed has nobody to tell where they have already been, and the reader is the only person who will ever load these pages.

It is also inconsistent in a way that reads as a bug rather than a signal: it applied to the thirteen links nobody had styled and to none of the ninety-eight that had been, so the same kind of link went purple in one place and not in another.

## The rule is a zero-specificity default

```css
:where(a, a:visited) { color: var(--accent); }
```

`:where()` contributes no specificity, so this is a **default and not an override**. `.card-provenance` is a single class, (0,1,0), and still wins. A plain `a, a:visited` is (0,1,1), the same as every `.foo a` in the file, and source order would then decide which of the two applied — at the top of the file it would have lost to all of them, and anywhere else it would have silently taken the colour away from whichever ones it happened to precede.

`a` on its own already matches a link in either state, so the `:visited` in there matches nothing the first half does not. It is written out because the rule only holds while nothing else singles `:visited` out.

Checked mechanically: every anchor on twelve pages, before and after. All 13 changed from `rgb(158, 158, 255)` to `rgb(122, 162, 247)`, which is `--accent`. Nothing that already had a deliberate colour moved.

## Hovering is checked at the source, because the browser will not answer

The visited half cannot be verified live, and finding that out is part of the record. A browser **refuses to report a visited link's real colour** to `getComputedStyle` — it returns the unvisited one — and an automation profile records no history for `:visited` to match against. The positive control says so: injecting `a:visited { color: #ff0000 !important }` over a link whose target had just been loaded left it blue. So a screenshot would have proved nothing, and saying "it looks right" would have been a claim with no measurement under it.

The property is not "it looked right once" but "no rule distinguishes the two states", which is a fact about the files. `tests/test_stylesheets.py` reads core's sheet and every correspondent's, collects every selector mentioning `:visited`, and asserts the only one is the rule above. It fails when a plugin adds one, which is the case that would otherwise never be noticed, since a plugin's sheet is a separate file loaded onto the same page.

## The hover default sits last in the file, and that is load-bearing

```css
a:hover { color: var(--text); }
```

This is the one rule in `style.css` that depends on **where** it sits. `a:hover` is (0,1,1), the same specificity as every `.foo a` giving a link its resting colour, so source order decides: at the top it loses to all of them and hovering does nothing. At the bottom it beats a resting colour and still loses to any component's own `.foo a:hover`, which is (0,2,1). A component with an opinion about hovering keeps it; every other link gets this.

`--text` rather than `--accent` because the default resting colour is already `--accent`, and `.glance-retry` had set the precedent: what is accent at rest goes to text on hover.

An underline was the first idea and it does not work. Every one of the thirteen links is **already underlined at rest** — nothing sets `text-decoration: none` on them — so an underline-on-hover default would have been invisible on exactly the links that needed it. That is why the default is a colour.

One component came out inert: `.section-sources` (further reading) is `--text` at rest with no hover of its own, so a default hover to `--text` would change nothing. It takes the opposite swap beside its own rule, which is what the nav and the attribution line already do.

Verified in the browser at 2560px: the week arrow and the stats link go `rgb(122, 162, 247)` → `rgb(232, 230, 225)`; a further-reading link goes the other way, to accent; `.site-nav a` keeps its own hover and is untouched.

## Rejected

- **`a:visited { color: inherit }` on its own.** `inherit` takes the *parent's* colour, not the link rule's, and it would have left the thirteen links UA-blue when unvisited and parent-coloured once opened. Clicking would still have changed the colour, which is the thing being removed.
- **Fixing the thirteen links one at a time.** Thirteen edits are thirteen separate things to audit, and the fourteenth link written next week is back to browser blue. One default is a single object to check, and the test above checks it.
- **A brighter accent as a new hover token.** It would give every link the same hover regardless of its resting colour, at the cost of a third link colour the theme does not otherwise use. Two existing precedents (`--text` ↔ `--accent`) already cover both directions.

## Consequences

- A link with no styling of its own is `--accent` and hovers to `--text`. That is now a real default, so a new page gets a themed link without anyone remembering to add one.
- Adding `:visited` anywhere fails the suite. The message says why.
- `.card-link` changed colour and nothing moved on screen: it is an empty stretched anchor with no text to paint.
