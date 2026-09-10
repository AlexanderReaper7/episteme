# 0053. The header nav folds into one button, and stays one list

- Date: 2026-08-31
- Status: live-verified 2026-08-31 in Firefox at 390x844 and 2560x1400
- Rule: below 40rem the header's links move into a panel behind a burger. There is **one** `<nav>` in the markup and the menu is a second layout of it, never a second copy. The state is one class the media query is the only reader of, so nothing has to be undone when the window grows.

## Context

At 390px the three links (Glance, Tune, Admin) did not fit beside the wordmark and wrapped onto a second row, taking 30px of a 844px screen and pushing every card down. Measured 2026-08-31: the inline row needs **463px** - 284 of nav, 99 of wordmark, 16 of gap, 64 of padding.

## Decision

**One list.** The obvious implementation is a second `<nav>` for the narrow layout, and it is the one to refuse: both render correctly, and the next route added goes into one of them. That failure is invisible until someone opens the app on a phone and cannot reach the page. `tests/test_header_menu.py` asserts each `href` appears exactly once in the rendered document.

**A `<button>`, not a `<details>` and not an anchor.** A closed `<details>` hides its own content, which is precisely what the wide layout must not do - and forcing it open above 40rem needs script anyway, so the element buys nothing. Every anchor on the page is boosted, so an anchor would navigate.

**40rem, a width query.** The card's stacking asks `orientation` because its question is the proportions of the space (0052); a nav's question is how many characters fit beside the wordmark, and a phone in landscape has the room. 40rem folds the row while it still fits rather than at the pixel where it breaks: between 463px and 640px the links are on one line, 40px from the wordmark, at 0.85rem uppercase - a tap target the size of the word it sits on. It is also where the page gutter narrows, so the two agree instead of staggering.

**The panel is opaque, not the app's frost.** `--frost-bg` + `--frost-blur` is one recipe for the whole app, and this is the one surface it cannot serve. Verified in the browser: with the panel's own background removed, the card photo behind it comes through **completely unblurred** while `backdrop-filter: blur(10px)` computes on the element. The header is already a backdrop root and the panel hangs outside its box, so a nested filter has nothing to sample. A menu over a bright photo has to be readable, so it is `--card-bg` with a shadow - a surface, not glass.

**Every way of closing it is one click handler.** A boosted link swaps `#main-content` and never touches the header, so without an explicit close the panel would stay open over the page it just opened. A click on the button toggles; a click anywhere else closes, which covers both the link (that click *is* the navigation) and dismissal. Escape closes and returns focus to the button. Closing on `htmx:load` was rejected: it looks like the general answer and is wrong for the reason it looks right - a polling fragment anywhere on the page fires it, and the menu would shut under the reader's finger.

## Consequences

- The header is one row at 390px again, which gave each card ~2 more lines of summary (0052's clamp is measured against the window minus the header).
- One glyph added to the sprite, `menu` -> Carbon `menu`. The open state renders `remove`, the same close glyph the chips use, and the swap is two elements in the markup with a CSS rule - not a script rewriting `<use href>`.
- The admin shell's own sidebar nav is untouched; it has its own row-collapse breakpoint.
- `aria-expanded` stays `"true"` if the window is widened while the menu is open. The button is `display: none` there and so is not exposed, and the next click on it corrects the attribute.
