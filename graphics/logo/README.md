# Episteme — primary mark

Design doc for the Episteme project icon. This folder holds the working
prototypes, and `build_svg.py` distils them into the shipped static assets.

---

## 1. The idea

Two elements, and the relationship between them is the whole logo:

| Element | Stands for | Reads as |
| --- | --- | --- |
| **The weave** | *doxa* — belief, opinion, the undifferentiated stuff everyone has | An unbounded field of information. No origin, no terminus; it runs past every edge of the frame. Ambient because it genuinely is — it is there whether anyone is looking. |
| **The obelisk** (vertically elongated octahedron) | *episteme* — justified knowledge | Structure. The place where the field acquires orientation and becomes something you can navigate by. |

The name is doing the work. Plato defines *episteme* against *doxa*, and the two
shapes map onto that distinction exactly.

Three claims the composition has to make, in order of importance:

1. **The obelisk is embedded in the weave, not placed on top of it.** It has mass
   *within* the fabric, deforms it locally, and strands pass in front of it. This
   is the epistemically honest claim: knowledge is not imported from outside the
   world of information, it is a *densification* of it. A mark where the obelisk
   floated above the field would be claiming something we do not believe.
2. **Knowledge is what bends the field around it** — not what illuminates it.
   What has enough mass to reorganise it. This is why the gravity metaphor beat
   the lighthouse metaphor: "lighthouse shows the way" is a much less interesting
   claim, and a much more arrogant one.
3. **The field is undisturbed except locally.** The deformation is a well, not a
   flood. Influence falls off.

Vibe words, in priority order: **scientific, honest, humble.** Everything below
is downstream of those three. No glow, no accent colour, no frame, no
decoration that is not doing semantic work.

---

## 2. Fixed design decisions

These were decided and should not be silently revisited.

- **Octahedron, vertically elongated.** Solid and filled — a dense object with
  weight, not a wireframe.
- **Solid and diffuse, in greys.** No transparency, no visible internal lines, no
  glow. Shading comes from real facet normals against one light; PBR-ish
  parameters (`albedo` / `roughness` / `sheen`) exist so the surface can be tuned,
  but the default is matte.
- **The point of influence is the bottom tip**, not the body centre. Gravity pulls
  the weave toward that tip.
- **The weave is stationary; its strands ripple.** Earlier drafts drifted the whole
  grid like a scrolling data stream. That was replaced: ripples originate at points
  and travel outward, dispersing as they go. Stationary grid + travelling
  disturbance reads as *a field being informed*, not *a conveyor belt*.
- **Ripple origins use evenly distributed randomness, not uniform random.** The
  current implementation is Mitchell best-candidate sampling (blue noise):
  14 candidates per spawn, keep the one furthest from the last 7 origins. Pure
  `Math.random()` clumps and reads as a bug.
- **Perspective plane, fading into the distance**, and it must fade out before it
  would rise above the obelisk's midline — otherwise it competes with the
  silhouette.
- **The warp is *toward* the mass, not straight down.** Gravity, not drape.
  Everything about it is parametric so the same rig drives every animation state.
- **No frame.** Shapes on a transparent background.
- **Weave density scales with resolution.** ~5 visible cells at normal size; at
  higher resolutions, many more strands with varying thickness.

### Force model

The deformation is modelled as two forces, and each patch of strand carries both
a **mass** and a **charge**:

- **Gravity** — long range, attractive, sourced at the bottom tip. Falls off as
  `1 / (1 + (d/wellR)²)`.
- **Exclusion** — short range, repulsive, Pauli-flavoured. The weave is *forbidden*
  from entering the solid. This is what stops strands from clipping through the
  obelisk, and it replaced an earlier approach that just deleted the offending
  segments.

Mass and charge are **fields over the plane**, not per-strand constants. This is
load-bearing: a row and a column crossing at the same spot must resolve to the
same 3D point, or the two families visibly drift apart and the lattice stops
reading as one surface. (That bug happened; this is the fix.)

Mass also does real work in the motion — `a = F/m`, so heavier patches answer a
passing ripple less.

### Occlusion

The obelisk must behave like a 3D solid without being *rendered* as one at ship
time. Visibility comes from a ray/solid intersection test cast **from the camera**,
so it is a straight depth comparison: a strand point is in front if it is nearer
than the solid's near surface along that ray. Nothing gets deleted to look right —
an earlier version classified whole strand segments as occluded and removed them,
which chewed visible chunks out of strands.

---

## 3. Deliverables

A **static SVG** is the fallback and the base version. `build_svg.py` emits it:
a port of `episteme-3d.js`, function for function, with ripples dropped
(`field()` returns 0) and each strand reduced by Ramer-Douglas-Peucker to under
half a pixel of error. The mark is WIP: good enough to wear, not signed off.

On top of it, a set of animations whose *meaning* is tied to system state:

| State | Motion | Status |
| --- | --- | --- |
| **Normal operation** (default) | Weave stationary, ripples travelling steadily outward and dispersing | ← current focus |
| **Error** | Unmoving, or jagged/disrupted weave, red | not started |
| **Work happening** | Weave interacting significantly with the obelisk (exact form TBD) | not started |

### Sizes

| Target | Notes | Status |
| --- | --- | --- |
| Favicon, 16 to 32px | Obelisk only, or obelisk + 2 to 3 strands. The weave will not survive at this size, measured above. | open; the app-icon crop stands in |
| **Normal size**, site header / logo lockup | current focus | prototype; not in the header yet, WIP |
| App / social icon, ~512px square | | not started |
| README / docs | | not started |
| Full-size — 4K fullscreen, obelisk centred at ~25% height, weave spanning the full width below it | | not started |

`src/episteme/web/static/logo/episteme.svg` is the app-icon crop: a tighter
camera (`fov` 30, `camY` 0.9) so the field reads at small sizes, and the obelisk
*optically* centred (`center_y` 0.46) rather than sitting on a horizon at `h/2`,
which is a different number for an icon than for a full-bleed canvas. It is
generated straight into the static tree rather than into this folder, because it
is shipped: the Dockerfile copies `src` only, so a copy here could not be served
and could only go stale. It is currently doing two jobs at once, the tab icon and
the app icon this section is about. It is deliberately **not** in the site header:
the mark is WIP, and the wordmark stands alone until it is signed off.

Confirmed by measurement rather than by eye, and the reason the favicon row below
is still open: at 16 px the obelisk survives but the five waves merge into one
grey smudge, and the near-black left facets (`#2e3033`) lose their edge against
the app's true black. Zooming is not the fix - `ICON_CAMERA` is already at the
bound. At `fov` 30 the solid spans 79% of the frame height (top tip at y=14.7,
lower at 216.7 of 256); `fov` 26 already cuts the points off flat, which destroys
the one thing §2 fixes about the silhouette. The levers that remain are the wave
count and lifting the facets off black (`albedo` / `roughness`), both of which
are `Config` values and neither of which touches the solid.

**What is WIP about it is only the field.** The solid, its lighting and its
silhouette are the truth model's, unchanged; the weave is stood in for by five
stylised waves that dip under the tip (`strands=False, waves=5`), because the
solved weave at 256 px is the crossing-brightness and strand-breakage problems
of §6 rendered small. Flipping `strands` back to True and `waves` to 0 renders
the real one, which is what this file becomes when those are fixed.

**There used to be a second mark here**, the same solid standing in a neural
network instead of a weave, worn by the host agent. It left with llama-warden in
2026-09 (0057) and took `_net` and its own README section with it. The generator
was copied rather than shared, deliberately: see `graphics/README.md` in that
repository. The obelisk may drift apart now, and if it does that is a decision
somebody made in one of the two places rather than a copy that went stale.

---

## 4. Files

| File | What it is |
| --- | --- |
| `Episteme 3D.dc.html` | **Current prototype.** Full 3D truth model on canvas — real octahedron, real grid, real perspective, real occlusion. The intent is to get the geometry right in 3D and then *distil* it to 2D SVG paths. |
| `episteme-3d.js` | The logic class for the above, extracted from its inline `<script>`. |
| `build_svg.py` | **The distiller.** `uv run graphics/logo/build_svg.py` rewrites the SVG; `--list` says what it would write. One `Config` per mark, and anything overridden is stated at the call site so a diff against the prototype is a diff of the call. |
| `../../src/episteme/web/static/logo/episteme.svg` | Generated. The app icon, and the file the web app serves as its tab icon. |
| `Episteme.dc.html` | Earlier 2D iteration. |
| `Episteme v1 drift.dc.html` | First iteration, drifting/scrolling weave. Superseded — kept for reference. |
| `support.js` | dc-runtime. Generated; do not edit. |

### Why `episteme-3d.js` is loaded the way it is

dc-runtime reads the `textContent` of `<script data-dc-script>` and evaluates it
with `new Function("DCLogic", …)` — so `DCLogic` is a **function parameter, not a
global**, and the class cannot simply be declared in an external file. Instead
`episteme-3d.js` (loaded normally in `<head>`) assigns a factory:

```js
window.EpistemeObeliskWeave = function (DCLogic) {
  class Component extends DCLogic { /* … */ }
  return Component;
};
```

and the `data-dc-script` block is a one-line shim:

```js
const Component = window.EpistemeObeliskWeave(DCLogic);
```

**Caveat:** this works when the `.dc.html` is the document being opened. If it is
ever pulled in as a *sibling component*, dc-runtime fetches only the `<x-dc>`
template and the script text — the `<head>` tags are never executed, and the
factory would be undefined. Inline the logic again if that day comes.

---

## 5. Parameters

All exposed as dc-runtime prop editors, grouped by section. Defaults in bold.

**Weave** — `cell` **0.95** (0.4–2, world units between strands) · `gridPhase`
**0.5** (0–1; at 0.5 the lattice straddles the origin so the obelisk stands in an
open cell instead of being skewered by two strands) · `weaveColor` **#8DA6C0**

**Forces** — `gravity` **0.55** (0–1.6) · `wellR` **2.2** (0.8–6, falloff radius) ·
`exclusion` **0.1** (0–0.6, keep-out margin) · `massVar` **0.3** · `chargeVar` **0.3**

**Motion** — `rippleAmp` **0.16** *(currently dead — see §6)* · `rippleRate` **1**
(spawns/sec) · `rippleSpeed` **1.5** (crest velocity) · `rippleFade` **1.5**
(exponent on the swell-in/settle-out envelope, *not* a decay rate)

**Obelisk** — `obeliskY` **0** (−1.5–1.5; sits slightly *into* the weave) · `elong`
**2.4** · `girth` **0.42** · `albedo` **#9AA2AC** · `roughness` **0.75** · `sheen`
**0.06** · `lightAz` **0.9** · `edge` **0.2**

**Camera** — `camY` **0.85** · `camDist` **4.6** · `fov` **34**

---

## 6. Known issues

Ordered by how much they hurt.

1. **Strands still break.** Two causes, both fixable:
   - The front/back classification is per *sample*, so a run ends one sample
     before the true silhouette crossing. The gap is one sample step wide —
     several pixels near the camera.
   - The restored front-pass run recomputes its own gradient and line width from
     its own endpoints, so even a geometrically continuous strand shows a seam in
     colour and thickness where the two passes meet.

   Fix: bisect to the exact front/back crossing and carry fog/width from a shared
   parameterisation of the whole strand, not per run.

2. **Exclusion is not smooth — it shows as jagged kinks.** The push direction is
   the octahedron's analytic facet normal, `(sign(x)/Wx, sign(y−oy)/Hy,
   sign(z)/Wz)`, which is piecewise constant and *flips discontinuously* across
   each coordinate plane. Strands crossing x=0 or z=0 get a hard corner. Two more
   hard clamps compound it: the 0.45-cell lateral cap and the `vLim` vertical cap
   are both `min/max` clamps, i.e. C⁰ kinks wherever they bind.

   Fix: smooth the normal (p-norm SDF, or a smoothstep-weighted blend of the 8
   face normals, or a normalised numerical gradient), and replace both hard clamps
   with soft saturation (`tanh`). This is the real fix — note that **splines will
   not fix this**; a curve fitted through kinked samples still shows the kink.

3. **Ripple sources fire once.** Each origin emits a single expanding crest with a
   bell envelope over its life. They should be able to bob — emit a short *train*
   of pulses. Add a cycle count / source frequency and window a multi-cycle
   sinusoid instead of the current single `cos(q·1.7)`.

4. **Strands have only one degree of freedom.** The ripple displaces `p[1]` and
   nothing else. Real DOF options: displace along the local surface normal
   (from the field gradient) rather than straight up, which gives transverse *and*
   longitudinal motion for free; and add an in-plane radial term so the field
   compresses and rarefies like a P-wave.

5. **Bright spots where strands cross.** Two semi-transparent lines composited
   `source-over` accumulate alpha, so every intersection is brighter than either
   strand. See §7 for the fix — it is a rendering-model change, not a tweak.

6. **`rippleAmp` is dead.** `cfg()` reads it; nothing consumes it. Ripple
   amplitude comes from `0.65 + Math.random()*0.7` in `pump()`. Either multiply
   `R.amp` by `k.rippleAmp` or drop the slider.

7. **Constant width along a whole column.** `lineWidth` is computed from the run's
   *average* depth, but columns span z from −16 to +2.85 — a huge depth range. The
   near end should be visibly thicker than the far end; currently only the alpha
   gradient conveys that.

8. **Far field is nearly flat by construction.** `vLim = 0.8·camY·cell / d` squashes
   vertical displacement to ~0.04 world units at d≈18. It is there to stop
   perspective inversion, but it means distant ripples are invisible and it is one
   of the hard clamps in issue 2.

9. **Weave extent is hard-coded** (`EX = 14`, `ZF = −16`) rather than derived from
   the view frustum, and column count is `floor(EX/cell)`. At `cell = 0.4` that is
   ~31k strand samples per frame. Density should follow screen coverage, not a
   fixed world box.

---

## 7. Open questions

### Should the strands be splines?

**Yes, and it is close to required for the SVG deliverable.** No correctness
problem: the strands are already dense polylines (300 samples per row, 240 per
column), so a spline is just a *fit* to far fewer control points. Catmull-Rom →
cubic Bézier is the standard route; canvas has `bezierCurveTo`, SVG has `C`.
Fitting with Ramer–Douglas–Peucker followed by a least-squares cubic fit
(Schneider's algorithm) at ~0.3px tolerance should take ~300 samples down to
roughly 8–20 control points per strand.

Three caveats:

- Splines **do not** fix issue 2. Smooth the force field first, then fit.
- The front/back split must happen at the *exact* silhouette crossing **before**
  fitting, or the fit smooths across a discontinuity.
- Canvas gradients along a curved path are still linear in screen space. That is
  fine — the current code already projects the gradient onto the run's dominant
  axis.

### What does it take to hold 100+ fps?

Current per-frame cost is roughly **13,000 `fabric()` evaluations** (≈20 rows ×
301 + ≈30 columns × 241), each doing two projections, two value-noise lookups
(8 `Math.sin` calls for hashing), a ripple sum, an SDF, and an 8-plane ray test —
then every strand is stroked **twice** (base pass + front pass) with a
freshly-allocated canvas gradient carrying up to 36 stops.

In rough order of payoff:

1. **Hoist the per-ripple constants out of the per-point loop.** `bell`, `crest`,
   `sigma` and the `2.4/(2.4+crest)` term depend only on the ripple's age, not on
   the point — but they are recomputed for every one of the 13k samples, including
   a `Math.pow(Math.sin(…))`. Pure waste; probably 30–40% of the field cost.
2. **Separate simulation resolution from render resolution.** The field is smooth
   (once issue 2 is fixed), so evaluate on a coarse lattice and spline between.
   Sample adaptively by *projected arc length* — far rows currently get the same
   301 samples as near ones while covering ~10 screen pixels.
3. **Share one vertex grid between rows and columns.** They currently evaluate
   `fabric` at different points and duplicate the work near every crossing. A
   shared grid halves the cost *and* makes "rows meet columns" true by
   construction, so that bug can never regress.
4. **Cache the time-invariant deformation.** Gravity, the mass/charge noise and
   exclusion do not change between frames for a fixed obelisk and camera. Only the
   ripple term is dynamic. Precompute the static displacement on parameter change;
   per frame, add the ripple and reproject.
5. **Bound and cheaply cull ripples.** Cap the active list, and reject on *squared*
   distance before computing `hypot`.
6. **Replace the `sin`-based hash** in the value noise with an integer/`Math.imul`
   hash — 8 transcendentals per sample is the single densest cost in `fabric`.
7. **Screen-space bbox reject for the occlusion test.** Project the obelisk's
   bounding box once; samples outside it are trivially unoccluded, which skips both
   the 8-plane ray test and the entire second stroke pass for most of the weave.
8. **Fewer gradient stops.** 36 is far more than the eye resolves; 6–8 is identical
   on screen, and a run whose alpha range is under ~0.05 needs no gradient at all.

**And the fix for issue 5 falls out of the same work.** Render the weave into an
offscreen layer with **opaque** strokes — crossings then composite identically to
single strands, because there is no alpha to accumulate — and apply the depth fade
afterwards as a mask (`globalCompositeOperation = 'destination-in'` with a vertical
alpha gradient). Fog is very nearly a pure function of screen y for a ground plane,
so a linear gradient reproduces it. It translates directly to SVG as a `<mask>`
with a `linearGradient`, and it makes ripple energy a *width* channel rather than a
second alpha channel.

### Animating the shipped SVG

Do not try to push 13k points through the SVG DOM. Either:

- ship the static SVG as the fallback and drive the animation on canvas (or WebGL)
  for the header hero; or
- **spline the strands first** (§7.1) and animate ~50 paths × ~20 control points —
  about a thousand numbers per frame, which SVG handles comfortably at 100+ fps.

That second option is the strongest argument for splines: it is what makes a
single animated SVG deliverable feasible at all.
