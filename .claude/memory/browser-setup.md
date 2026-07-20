---
name: browser-setup
description: User's browser — Firefox at 150% zoom on a 4K monitor, with the Dark Reader extension
metadata:
  type: user
---

The user browses in **Firefox** with a **default 150% zoom** (they are on a 4K
monitor, so effective CSS viewport ≈ 2560px wide) and the **Dark Reader**
extension, which auto-darkens every site.

Implications when judging how a page looks for them:
- A "full width" impression is relative to ~2560 effective CSS px, not 1280 —
  narrow centered columns (e.g. the feed's `max-width`) read as much narrower to
  them than in a default-width screenshot. When reproducing their view via
  Playwright, emulate a wide viewport (~2560px) rather than the default.
- Dark Reader re-themes sites, but Episteme is already true-black OLED, so its
  own dark theme should look right *without* Dark Reader's transforms fighting
  it. Watch for cases where Dark Reader inverts/recolors something we already
  styled dark.
