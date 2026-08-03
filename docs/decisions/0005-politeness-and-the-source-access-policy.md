# 0005. Politeness toward sources is hard, and blocked sources are worked around without raising volume

- Date: 2026-07-17 (policy), 2026-07-18 (per-source pacing)
- Status: accepted
- Rule: all source HTTP goes through `polite_get`. Never raise request volume. Never fetch a robots.txt-disallowed path.

## Context

Episteme polls other people's servers continuously and forever, for one reader. The cost of being impolite is borne by someone who gets nothing out of this, and the site that blocks us is a site we lose permanently.

Separately, some publishers block or degrade an honest client, so "be polite" and "actually get the content" pull against each other.

## Decision

Every source request goes through `ingest/http.py:polite_get`:

- one global throttle, minimum 2s gap, drawn ~N(3s, 1s), applied before every request
- conditional GETs, with ETag and Last-Modified persisted on `Source`
- a 429 sets a persisted per-source `cooldown_until`, honoring `Retry-After`

The user's standing policy for a source that fails or returns degraded content: use any **non-destructive** means to make it work. Non-destructive excludes spamming and high volume. TLS impersonation, and a headless browser if it comes to that, are in scope. Two hard lines: never raise request volume, the throttle and the cooldowns stay; and never fetch a robots.txt-disallowed path, reading robots.txt's literal bytes rather than a summary of them.

Two per-source keys were added after repeated Phys.org 429s at global pacing: `min_request_gap_seconds` adds per-host spacing on top of the global throttle, leaving other hosts unaffected, and `fetch_interval_minutes` makes scheduled `ingest_all` poll that source less often than the cron fires. A manual `ingest_source` defer still forces a fetch.

## Consequences

New adapters must use `polite_get`, which returns a transport-agnostic `FetchResponse` and raises `FetchError` on status >= 400.

Never run `docker compose down -v` casually: re-ingesting re-fetches every article from every source, which converts a careless volume command into exactly the thing this policy forbids.
