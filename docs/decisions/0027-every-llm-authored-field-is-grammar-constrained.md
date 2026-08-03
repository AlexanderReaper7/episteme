# 0027. Every LLM-authored field is grammar-constrained, including the chart spec

- Date: 2026-08-02
- Status: accepted
- Rule: no `dict[str, Any]` at a model boundary. A DSL in a string gets a validator instead.

## Context

Post 427's chart section stored:

```json
{"data": [...], "title": "...", "x_axis": "City", "y_axis": "% Obscuration"}
```

That is not Vega-Lite. `data` must be `{"values": [...]}`, `x_axis` and `y_axis` are not properties, and there is no `mark` or `encoding`. `vegaEmbed` rejected it, the figure collapsed to its caption, the caption was `""`, and the section rendered as **nothing**, for two weeks, at `quality_score` 9.

The root cause is one line: `spec: dict[str, Any]` was the only field in the entire `Section` union that escaped the grammar. Every shape-constrained field came back well-formed. The one unconstrained field came back as invented syntax.

## Decision

`spec` is now `ChartSpec`, a typed Vega-Lite subset: marks bar, line, point and area; x and y plus optional color; inline `data.values`. Grammar-constrained and pydantic-validated like everything else at this boundary, with `agent.request_validated`'s repair loop putting a rejection back in front of the model.

Beyond shape, two validators check the things a schema cannot:

- `ChartSpec._channels_match_the_data` checks that every encoding channel names a key the rows actually have, because a flawless spec still draws an empty frame if `encoding.x.field` matches nothing. It **intersects** the row keys rather than unioning them, since a field only one row carries would otherwise validate and draw one bar out of N, and it reports which of "no row" and "only some rows" it hit.
- `DiagramSection` requires the Mermaid source to open with a recognised diagram type, since Mermaid picks its parser from that keyword and without one the whole figure is a syntax error. Directives (`%%{…}%%`) and frontmatter are skipped first, and the frontmatter skip covers the **body**, not only the `---` fences, because skipping only the fences left `title:` reading as the diagram declaration and failed valid Mermaid. An unclosed fence is its own error.

A DSL inside a string is the one thing the grammar cannot constrain, which is exactly why the screenshot survives for diagrams (see 0028).

## Rejected

A sub-agent for charts, asked and decided the same day. A second conversation generates the same free-form JSON at the cost of re-prefilling the grounding set the writer already holds warm, and on a 10 GB card a parallel slot *divides* `--ctx-size` between KV caches. Constraining the grammar costs nothing and fixes the actual cause.

## Renderer fixes found only by looking in a browser

`mode: "vega-lite"` is pinned in the `vegaEmbed` call, since it otherwise infers the parser from `$schema`, which a generated spec has no reason to carry.

A chart is fitted to the article column by a **measured pixel width**, not `width: "container"`. Container sizing reads the target's `offsetWidth`, which measured **0** here and collapsed the chart to nothing, a worse failure than the 200px default it was meant to fix.

## Verified live, 2026-08-02

Post 427's spec was repaired in place by one SQL statement deriving the new shape from the old (`data` into `data.values`, `x_axis` and `y_axis` into axis titles, zero numbers retyped), validated through the new `ChartSection`, and confirmed drawing: 8 bars, all city labels, `render-failed` absent, SVG 736px in a 736px column, nothing clipped, no page overflow.

**The numbers themselves are not vouched for.** The post is about a European eclipse and the chart lists North American cities. Re-running `qa` on 427 will judge that against the sources with a screenshot in hand.
