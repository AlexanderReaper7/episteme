# Choosing the icon set (2026-08-02)

Episteme had no symbol language at all: no SVG anywhere in the app, no icon macro, no icon font, not even a favicon. The whole glyph vocabulary was `·`, `×`, `←`, `→`, `—` and every control was a word. This is the record of picking a set — the method and the numbers. The conclusion lives in `CLAUDE.md`.

## Method

The choice was made **by eye, on the real theme, at real size, inside the real buttons** — not from descriptions. A throwaway `/admin/icons` page (since deleted, along with its route and its generator) rendered the 34 glyphs this site actually needs from six packages side by side, plus a mocked feedback row and a mocked admin row per set, plus a size ramp at 13 / 15 / 20 / 24 px, plus an animation lab.

The inventory was resolved against each package's real file listing before any comparison, so no set was judged on glyphs it does not have. All six resolved all 34, though Iconoir needed second-choice matches in two places (`power` → `system-shut`).

## The candidates

| Set | Package | Version | License | Glyphs | Geometry |
|---|---|---|---|---|---|
| Lucide | lucide-static | 1.28.0 | ISC | 2007 | 24×24, stroke 2, round caps |
| Tabler | @tabler/icons | 3.46.0 | MIT | 5130 | 24×24, stroke 2, round caps |
| Iconoir | iconoir | 7.11.1 | MIT | 1383 | 24×24, stroke 1.5, round caps |
| Phosphor | @phosphor-icons/core | 2.1.1 | MIT | 1512 | 256×256, filled paths |
| Remix | remixicon | 4.9.1 | *see below* | 3229 | 24×24, filled paths |
| **Carbon** | **@carbon/icons** | **11.85.0** | **Apache-2.0** | **2725** | **32×32, filled paths** |

Heroicons was in the running and was dropped by the user before the comparison page was built.

**Remix's license needs a footnote.** Its `package.json` says `Apache-2.0`, but the shipped `License` file is a custom "Remix Icon License v1.0" dated January 2026. Two sources of truth disagreeing about the terms is itself a reason to prefer something else. (This was stated as Apache 2.0 earlier in the discussion and corrected.) Carbon's Apache-2.0 is the same in both places.

Licensing was not decisive — the user's call: *"we dont really need to worry about license. its just a personal project anyway."* All six are permissive enough for this. It is recorded because the answer would differ if this were ever distributed.

## Why Carbon

It is IBM's dashboard set. Coolest and most clinical of the six, strongest operator vocabulary (`flow`, `renew`, `branch`, `circle-dash`, `settings--adjust` all exist as real glyphs rather than approximations), and the mocked admin row was the one that looked like a system rather than a toy. That matters here: half this site is a dense operator surface where the glyph has to be *accurate*.

## What it costs

- **No draw-on animation, ever.** Carbon's glyphs are filled compound paths; there is no stroke to dash. The stroked sets (Lucide, Tabler, Iconoir) can animate a `stroke-dashoffset` draw-on and Carbon structurally cannot. Accepted — the user chose press + swap + spin, none of which need a stroke.
- **The optical-size advantage does not apply.** Carbon advertises glyphs redrawn per size rather than scaled, at 16/20/24/32. Measured against our 34: the 16px grid has 68 files and **0** of ours, 20px has 9 and **0**, 24px has 8 and **0**, 32px has 2590 and **all 34**. So `svg/32` is not a choice, it is the only grid that has the icons, and every glyph on this site is a scaled 32.
- **Cooler and more corporate** than Lucide/Phosphor. Deliberate, but it is a register, and it is the register the whole site now speaks in.

## What Carbon gives back

`--filled` twins for 14 of the 34, including every control with a pressed state: like, dislike, save, hide-source, show-source, pin. That is what makes the outline→solid swap possible without hand-drawing anything, and it is why `FILLED` in `tools/build_icon_sprite.py` is a short list rather than all of them.

## Animation: what was verified, not assumed

The user's position, verbatim: *"taking 'no dark patterns' to mean no animation is grossly overkill … animations only increase the livlyness and quality of the site and do not trigger a dopamine kick."* Constraint: **code-driven, preferably no JS, and open.** A celebratory "Good Job!" with confetti is the line.

Four mechanisms were built and tried in the browser:

1. **Draw-on** — `stroke-dashoffset` from the path length to 0. *The question was whether a sprite can animate at all*, since `<use>` puts the geometry in a shadow tree and individual paths inside a `<symbol>` **cannot** be selected by CSS. Answer: `stroke`, `stroke-dasharray`, `stroke-dashoffset` and `color` are *inherited* SVG properties, so setting them on the host `<svg>` reaches through. Verified live. Stroked sets only — hence unavailable under Carbon.
2. **Press** — transform on `:active`. Shipped.
3. **Swap** — two stacked `<use>`s cross-fading. Shipped.
4. **Spin** — rotates the host element, so it is independent of how the symbol is drawn. Shipped, for running/loading states only.

**Line MD** (the animated-icon set) was considered as a source and rejected: its own README concedes that triggering is "not always trivial" with SVG Animations Level 2 — those icons play on *appear*, not on hover or press, which is the wrong event for controls.

The press animation is the one that matters on the feedback row, and there is a non-obvious reason it works: those controls replace themselves over htmx, so a CSS transition on the *result* state would never play (the element is gone). The press plays on the old element, before the swap — exactly when the reader is looking.

## Two things that bit during the build

- **Carbon SVGs declare no `fill` and no `stroke` at all**, which means the SVG default: solid black, invisible on a true-black theme. Every glyph rendered as nothing. The fix is not to patch Carbon but to impose a colour contract in the generator (`normalize()` in `tools/build_icon_sprite.py`) for whatever package is fed to it, reading stroked-vs-filled off the geometry as shipped rather than trusting the set's reputation.
- **`<svg hidden>` does not hide an inline SVG.** The sprite laid out at the default 300×150 and shoved the page down. Only an explicit `display: none` works — `.icon-sprite` in `style.css`.

## Sizing

Icons are sized in `em` so they inherit from the control they sit in and follow browser zoom and OS scaling for free. But `1em` reads *smaller* than the words beside it, because a glyph fills the whole em box while text only reaches cap height. The user runs a 4K 48" display at 125% Windows scaling and 150% Firefox zoom and asked for noticeably larger than the ~15px first proposed, so the ratio is a single site-wide knob, `--icon-scale` (default `1.5`), rather than a per-control size.
