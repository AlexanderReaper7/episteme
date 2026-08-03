# 0006. Two HTTP transport modes, chosen per source, changing only how we look

- Date: 2026-07-18
- Status: accepted
- Rule: `http_mode` is per-source config. Impersonation changes the fingerprint, never the volume.

## Context

Phys.org returned 429 to an honest httpx client regardless of user agent. Its detector is its own rather than Cloudflare's, there is no `cf-ray` header, and it fingerprints the TLS handshake. No amount of politeness fixes a client that is rejected before it says anything.

## Decision

`polite_get` has two modes, selected per source by the `http_mode` config key:

- `polite`, the default: honest httpx with a truthful user agent
- `impersonate`: curl_cffi presenting a real Chrome TLS/JA3 fingerprint, profile chosen by `impersonate_profile`

`worker/tasks.py` auto-escalates a source blocked with 403 or 429 in `polite` to `impersonate`, retries once, and persists the mode that worked.

Adapters never see raw httpx. `polite_get` returns a transport-agnostic `FetchResponse` or raises `FetchError`, and `SourceAdapter.extract(item, source)` takes the source so it can honor that source's mode.

## Measured

Against Phys.org: honest httpx 429s, curl_cffi gets HTTP 200 and the full feed. robots.txt, read as literal bytes, permits `/rss-feed/`.

## Rejected, and held in reserve

Routing through Mullvad to change the exit IP. The block is fingerprint-based, not address-based, so the tunnel would solve nothing today. It stays available if IP-based blocking ever appears.

## Consequences

This is the boundary the policy in 0005 draws: impersonation alters *how we look* and nothing else. The throttle, the cooldowns and the robots.txt rule apply identically in both modes.
