"""Read-only research tools handed to the writer's enrichment agent.

Two capabilities, both read-only — the whole injection posture rests on there being
NO tool that can take a consequential action, so a prompt-injection in fetched content
can at worst waste a fetch or degrade one article:

- `web_search(query)`  -> results from our own trusted SearXNG (not agent-controlled).
- `fetch_page(url)`    -> cleaned article text + the page's outbound links, for an
                          arbitrary agent-provided URL, behind an SSRF guard.

`fetch_page` is the untrusted surface, so it is hardened: scheme allow-list, an SSRF
check that every resolved IP is globally routable (blocks loopback / private / CGNAT /
link-local / ULA / cloud-metadata) with the connection pinned to the vetted IP (so a
rebinding DNS server can't pass the check and then serve a private address), manual
redirect following that re-checks each hop, a response-size cap, and text truncation
before anything reaches the model.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from urllib.parse import urlencode, urljoin, urlparse

import httpx
import trafilatura
from lxml import html as lxml_html

from ..config import settings
from ..ingest.http import FetchError, FetchResponse, polite_get

log = logging.getLogger("episteme.research")

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5
_MAX_RESPONSE_BYTES = 4_000_000
_MAX_LINKS = 40


class ResearchError(Exception):
    """A research tool refused or failed a request (bad URL, blocked host, HTTP error).
    Returned to the model as a tool error, never raised out of the loop."""


def _resolve_global_ip(host: str) -> str:
    """Resolve `host`, require every address it resolves to to be globally routable,
    and return one vetted address. Blocks loopback, private (incl. CGNAT 100.64/10),
    link-local (incl. 169.254.169.254 cloud metadata), ULA, reserved and multicast —
    the SSRF surface. The caller connects to the returned IP (DNS pinning): letting
    the HTTP client re-resolve at connect time would allow a rebinding DNS server to
    pass this check with a public address, then serve a private one."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ResearchError(f"cannot resolve host: {host}") from None
    addresses: list[str] = []
    for info in infos:
        ip_str = info[4][0].split("%")[0]  # strip zone id
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise ResearchError(f"host resolves to an unparseable address: {host}") from None
        if not ip.is_global or ip.is_multicast:
            raise ResearchError(f"host is not globally routable (blocked): {host}")
        addresses.append(ip_str)
    if not addresses:
        raise ResearchError(f"cannot resolve host: {host}")
    return addresses[0]


def _validate_url(url: str) -> tuple[str, str]:
    """Scheme + SSRF validation; returns (url, vetted IP to connect to)."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ResearchError(f"scheme not allowed: {parsed.scheme or '(none)'}")
    if not parsed.hostname:
        raise ResearchError("URL has no host")
    return url, _resolve_global_ip(parsed.hostname)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


async def _pinned_get(url: str, ip: str) -> FetchResponse:
    """GET `url` connecting to the already-vetted `ip`. The URL host is swapped for
    the IP; the real hostname travels in the Host header and (for https) as the TLS
    server name via httpx's `sni_hostname` request extension, so certificate
    verification still runs against the hostname."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if _is_ip_literal(host):  # nothing to pin — the vetted IP is the URL host
        return await polite_get(url, follow_redirects=False)
    ip_netloc = f"[{ip}]" if ":" in ip else ip
    host_header = host
    if parsed.port is not None:
        ip_netloc += f":{parsed.port}"
        host_header += f":{parsed.port}"
    pinned = parsed._replace(netloc=ip_netloc).geturl()
    return await polite_get(
        pinned,
        follow_redirects=False,
        extra_headers={"Host": host_header},
        extensions={"sni_hostname": host} if parsed.scheme == "https" else None,
    )


async def _guarded_get(url: str):
    """polite_get with the SSRF check applied to the initial URL and every redirect
    hop (auto-follow is disabled so a public->private redirect can't slip through),
    each connection pinned to the IP that passed the check. Transport failures (DNS,
    connect, TLS, timeout) surface as ResearchError — a tool error handed back to
    the model, never an exception that aborts the whole research loop."""
    current, ip = _validate_url(url)
    for _ in range(_MAX_REDIRECTS + 1):
        try:
            response = await _pinned_get(current, ip)
        except (httpx.HTTPError, OSError) as exc:
            raise ResearchError(f"fetch failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                return response
            current, ip = _validate_url(urljoin(current, location))
            continue
        return response
    raise ResearchError(f"too many redirects (> {_MAX_REDIRECTS})")


def _extract_links(raw_html: str, base_url: str) -> list[dict[str, str]]:
    """Absolute http(s) outbound links with their anchor text, de-duplicated. This is
    how the agent finds a source's "Read more" link (which trafilatura's content
    extraction drops)."""
    try:
        tree = lxml_html.fromstring(raw_html)
    except Exception:
        return []
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for anchor in tree.xpath("//a[@href]"):
        href = urljoin(base_url, anchor.get("href", "").strip())
        parsed = urlparse(href)
        if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
            continue
        text = " ".join(anchor.text_content().split())[:120]
        if not text or href in seen:
            continue
        seen.add(href)
        links.append({"url": href, "text": text})
        if len(links) >= _MAX_LINKS:
            break
    return links


async def web_search(query: str) -> list[dict[str, str]]:
    """Query our trusted SearXNG JSON API. Not SSRF-guarded: the endpoint is our own
    configured instance, not an agent-provided URL."""
    params = urlencode({"q": query, "format": "json"})
    url = f"{settings.searxng_url.rstrip('/')}/search?{params}"
    try:
        response = await polite_get(url, extra_headers={"Accept": "application/json"})
        response.raise_for_status()
        data = json.loads(response.text)
    except (FetchError, json.JSONDecodeError, httpx.HTTPError, OSError) as exc:
        raise ResearchError(f"search failed: {exc}") from exc
    results = []
    for r in data.get("results", [])[:8]:
        results.append(
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": (r.get("content") or "")[:300],
            }
        )
    return results


async def fetch_page(url: str) -> dict:
    """Fetch an arbitrary URL and return cleaned article text plus outbound links.
    All guards in this module apply. Text is truncated to `enrich_fetch_char_limit`."""
    response = await _guarded_get(url)
    try:
        response.raise_for_status()
    except FetchError as exc:
        raise ResearchError(f"HTTP {exc.status_code} fetching {url}") from exc
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise ResearchError(f"response too large (> {_MAX_RESPONSE_BYTES} bytes)")
    text = trafilatura.extract(response.text, include_comments=False, favor_recall=True) or ""
    return {
        "url": url,
        "title": _page_title(response.text),
        "text": text[: settings.enrich_fetch_char_limit],
        "links": _extract_links(response.text, url),
    }


def _page_title(raw_html: str) -> str:
    try:
        tree = lxml_html.fromstring(raw_html)
        titles = tree.xpath("//title/text()")
        return " ".join(titles[0].split())[:200] if titles else ""
    except Exception:
        return ""
