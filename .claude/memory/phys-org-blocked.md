---
name: phys-org-blocked
description: Episteme — why Phys.org needs http_mode=impersonate
metadata:
  type: project
---

Phys.org (seed source) runs its OWN bot-detector (no cf-ray header) that fingerprints
the TLS handshake: every honest httpx request 429s regardless of User-Agent, and a
browser User-Agent alone does NOT help (verified — the earlier `disguise_ua` httpx
tier was useless here). curl_cffi with a Chrome TLS fingerprint gets HTTP 200 and the
full feed. Its robots.txt (read literally, not summarized) permits `/rss-feed/`.

So its Source row carries `config.http_mode = "impersonate"`, and a live run through
the container ingested 30/30 items with full extracted text (2026-07-17). Rapid
repeats still 429 (genuine rate-limit) but back off to 200 when spaced — the normal
throttle/cooldown handles that. If IP-based blocking ever appears, the box has Mullvad
(WireGuard tunnel up but deprioritized, metric 5000 vs LAN 30) as an exit-IP lever.
See [[source-access-policy]].
