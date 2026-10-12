# 0063. A feed of informative news, not only science, written in its source's language

- Date: 2026-10-12
- Status: decided 2026-10-12, built, not yet watched running (docs/verification.md)
- Rule: every prompt frames the feed as "high-quality, informative news". A post and its card are written in the language of the story's primary item, which its source declares as `config["language"]` (`models.LANGUAGES`; absent means English). Topic tags stay English. A source's first fetch keeps only the last `cluster_window_days`.

## Context

The reader asked for sources outside science: hardware news of the VideoCardz kind, AI model releases (open-weight and frontier), and reputable Swedish news without clickbait or drama. Three things in the code stood in the way.

- Every prompt called this a "science-and-learning feed". Triage reserved "write" for "real science/learning value", sent "product news" to aggregate, and skipped "pure speculation". Hardware leaks and Swedish domestic news would have been aggregate cards or skipped.
- Nothing named an output language. A Swedish item would have produced English or Swedish depending on drift.
- `ingest_source` passed `last_fetched_at` as `since`, which is NULL for a new row, so a first fetch kept the whole feed and `RssAdapter.extract` fetched every page in it. Measured on 2026-10-12: OpenAI's feed holds 1287 entries, Hugging Face's 876, and SVT ekonomi's 100 reach back to April.

## Decision

The reader chose to widen the framing to "high-quality, informative news" rather than keep science and let the rest aggregate. Triage now counts a significant release (a model, a chip architecture) and consequential events as "write", credible leaks of concrete specifications as "aggregate", and drama, isolated crimes and accidents, and deals as "skip".

The language follows the source, and the reader chose a declared one over letting the model judge. `Source.language` maps the config code to a name, `pipeline.story_language` reads it off the primary item, and the writer, QA and both summarizers get it through a `{{language}}` slot. English is said explicitly too, because a model given Swedish material drifts into Swedish unasked. The primary item decides a mixed cluster because it is what the card links to: a Swedish card that opens an English page reads as a different story. `_story_items` now orders by `PRIMARY_ITEM_ORDER` so its first item is that one; it had ordered by `published_at` alone, which disagreed on a tie.

Tags are English whatever the language, so `swedish politics` and its Swedish spelling do not become two topics a preference has to find.

A first fetch is bounded by `cluster_window_days` (`tasks._since`). An item older than the window could only start a story of its own, so a separate setting would be a second number for the same boundary.

## Sources added the same day

Tech: TechPowerUp, Tom's Hardware, Chips and Cheese, ServeTheHome, SweClockers (sv), Phoronix, LWN, IEEE Spectrum, Ars Technica Gadgets. AI: Google DeepMind, Mistral, OpenAI, Hugging Face, Interconnects, Sebastian Raschka, Simon Willison, Ars Technica AI, Import AI. Swedish (sv): SVT inrikes, ekonomi, vetenskap and Väst, forskning.se, and Sveriges Radio's Ekot, added the same evening.

Ekot's robots.txt names `Claude-User` and `ChatGPT-User` and disallows them, while its `*` group permits `/artikel/`. The reader ruled that its `*` group is what applies to this client: one private reader, not a crawler, polite where a site allows it and impersonating where it blocks (0005, 0006). Akamai 403s the honest client on every article, so the row starts at `impersonate`. The page renders its text in script and extracts to its title alone, 65 characters, while the feed summary carries the whole story. `pipeline._plain_text` used to prefer any extraction; it now takes whichever of the two is longer. A browser renders the same three sentences the feed already has, measured with Playwright the same day, so a browser buys nothing there.

Left out, and why:

- VideoCardz: Cloudflare answers 403 on `/feed`, `/feed/rss2`, every category feed and both sitemaps, to every client measured: honest httpx, curl_cffi as Chrome and as Firefox, and Playwright's Chromium. The homepage and article pages answer 200 to curl_cffi and 403 to Playwright. No feed transport can reach it; only an adapter that reads the homepage as its list could.
- The Decoder: robots.txt disallows `*/feed/` for every agent.
- SVT's main feed: 56 of its 100 entries were regional news from all of Sweden, plus sport. The section feeds carry none of that. SVT utrikes was the reader's choice to leave out.
- DN and SvD: paywalled, so the writer's fetch reads a paywall; SvD's feed carries no summary at all.
- Anthropic, Meta AI, r/LocalLLaMA: no feed (404, 404, Reddit 403). Qwen's blog feed stopped in 2025-09.

## Rejected

- Keeping the science framing. The new sources would have been a headline stream, which is not what was asked for.
- Detecting the language from the text. A declared value per source is one row to read and test; a detector is a dependency that guesses on a 200-character snippet.
- A separate `first_fetch_lookback_days`. See above.

## Consequences

A reader-requested URL goes through the `manual` source, whose config is empty, so a Swedish page asked for by hand is written in English.

The prompts now carry every topic, so a science story competes with a Swedish budget story for the same write slots. The ranking, not the framing, decides which gets written first.
