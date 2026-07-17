---
name: source-access-policy
description: Episteme — how far to go to make a blocked/degraded news source work
metadata:
  type: feedback
---

For Episteme sources: if a source fails or returns degraded content to the polite
HTTP client, use any **non-destructive** means to make it work — where
non-destructive explicitly excludes spamming / high request volume. TLS-fingerprint
impersonation (curl_cffi), and a headless browser if needed, are in scope.

**Why:** The user gave this as a hard policy (2026-07-17) after Phys.org 429'd every
honest request. It's a single-user personal reader pulling content publishers
syndicate via RSS, at polite volume — a reasonable dual-use case, not scraping abuse.

**How to apply:** Keep the two hard lines the user's own "no spamming" implies and
spec §5 requires — never raise request volume (global throttle + cooldowns stay),
and don't fetch paths robots.txt disallows (but the user said don't over-trust a
robots.txt *summary* — read the literal bytes; verify with a raw fetch, not WebFetch's
LLM paraphrase). Mechanism built: per-source `http_mode` (`polite` default /
`impersonate` = curl_cffi Chrome TLS fingerprint) in `ingest/http.py:polite_get`,
with auto-escalation on 403/429 in `worker/tasks.py`. See [[phys-org-blocked]].
