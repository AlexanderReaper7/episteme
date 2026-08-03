# 0013. Content sections are a typed union, and media is a closed set like citations

- Date: 2026-07-19 (Phase 3)
- Status: accepted
- Rule: a section's shape is grammar-constrained. A media URL the writer did not receive as a candidate never reaches the page.

## Context

Phase 3's scope, decided with the user: media and rich sections now; micro-posts, minigames, a draft review UI and OpenAlex tracing later.

Rich sections mean the writer emits structured data, not prose, and structured data invented from nothing is the same failure as an invented citation.

## Decision

The `Section` union grew to nine members: `prose`, `key_points`, `image`, `video`, `quiz`, `chart` (Vega-Lite), `diagram` (Mermaid), `timeline`, `glossary`.

Media is closed-set. The writer's seed lists ingested `media_refs` as "Available media" with exact URLs. `pipeline.sanitize_media_sections` drops any URL that was not a candidate and stamps `attribution` and `source_url` from the database. QA's edits pass the same sanitizer via `qa._validate_section`.

Sections store the URL directly. There is no `MediaAsset` indirection, because the URL is already the identity and media is hotlinked, never stored (a standing constraint).

The enrich stage was folded into the writer (user decision, 2026-07-19): rich sections come out of the same agentic conversation as the prose, as guidance rather than quotas.

RSS extraction captures YouTube and Vimeo **embeds**, that is iframes rather than mere links, as `kind="video"` media refs.

## Rendering

One Jinja branch per type in `_sections.html`. A `video_embed` whitelist filter allows only youtube-nocookie and vimeo. Vega, Vega-Lite, vega-embed and Mermaid are vendored in `static/vendor/` and loaded **only** when the post actually contains a chart or diagram. `static/post.js` hydrates quiz, chart and diagram with dark themes, and a failed render collapses to the caption.

## Consequences

Nine branches that are total functions of their JSON is what later made the QA screenshot conditional (see 0028), and the one field that escaped the grammar, `spec: dict[str, Any]`, is what later broke a chart in production (see 0027).

Live-verified 2026-07-19 on story 291: the grammar handled the nine-type union on Qwopus with zero retries, the writer picked 1 of 3 offered images with a real caption and correctly skipped quiz and chart for a news-y item, and the vendor JavaScript was correctly *not* loaded.
