"""Research tools: the SSRF guard (the untrusted-fetch security boundary), DNS
pinning, transport-error containment, and outbound-link extraction."""

import socket

import httpx
import pytest

import episteme.research.tools as tools
from episteme.ingest.http import FetchResponse
from episteme.research.tools import ResearchError, _extract_links, _validate_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",  # loopback
        "http://10.0.0.5/x",  # private
        "http://192.168.1.10/",  # private
        "http://169.254.169.254/latest/meta-data",  # cloud metadata (link-local)
        "http://100.64.0.1/",  # CGNAT / Tailscale range
        "http://[::1]/",  # IPv6 loopback
        "ftp://example.com/x",  # scheme not allowed
        "file:///etc/passwd",  # scheme not allowed
        "https:///nohost",  # no host
    ],
)
def test_validate_url_blocks_ssrf_and_bad_schemes(url):
    with pytest.raises(ResearchError):
        _validate_url(url)


def test_validate_url_allows_public_ip_and_returns_vetted_ip():
    url, ip = _validate_url("http://8.8.8.8/")
    assert url == "http://8.8.8.8/"
    assert ip == "8.8.8.8"


async def test_fetch_page_pins_connection_to_vetted_ip(monkeypatch):
    """The DNS-rebinding defense: the request must go to the IP that passed the SSRF
    check (URL host swapped for the IP), with the real hostname preserved in the Host
    header and as the TLS server name — never re-resolved by the HTTP client."""
    monkeypatch.setattr(
        tools.socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    captured = {}

    async def fake_get(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FetchResponse(200, b"<html></html>", "<html></html>", httpx.Headers())

    monkeypatch.setattr(tools, "polite_get", fake_get)

    page = await tools.fetch_page("https://example.com/article")

    assert captured["url"] == "https://93.184.216.34/article"
    assert captured["kwargs"]["extra_headers"] == {"Host": "example.com"}
    assert captured["kwargs"]["extensions"] == {"sni_hostname": "example.com"}
    assert page["url"] == "https://example.com/article"


async def test_fetch_page_transport_error_becomes_research_error(monkeypatch):
    """A dead host must surface as a tool error the model can react to, not an
    exception that aborts the whole research loop (httpx errors are not OSError)."""

    async def boom(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(tools, "polite_get", boom)
    with pytest.raises(ResearchError, match="fetch failed"):
        await tools.fetch_page("http://8.8.8.8/x")


async def test_web_search_encodes_query(monkeypatch):
    seen = {}

    async def fake_get(url, **kwargs):
        seen["url"] = url
        return FetchResponse(200, b"", '{"results": []}', httpx.Headers())

    monkeypatch.setattr(tools, "polite_get", fake_get)

    assert await tools.web_search("black holes & M87 #EHT") == []
    assert "q=black+holes+%26+M87+%23EHT" in seen["url"]
    assert "format=json" in seen["url"]


async def test_web_search_transport_error_becomes_research_error(monkeypatch):
    async def boom(url, **kwargs):
        raise httpx.ReadTimeout("searxng down")

    monkeypatch.setattr(tools, "polite_get", boom)
    with pytest.raises(ResearchError, match="search failed"):
        await tools.web_search("anything")


def test_extract_links_keeps_outbound_anchor_text():
    """The JWST case: a 'Read more' anchor to a richer external page must survive,
    with its text, even though trafilatura's content extraction drops it."""
    html = (
        '<html><body>'
        '<a href="https://esawebb.org/images/potm2606a/">Read more about the image.</a>'
        '<a href="/relative/path">A relative link</a>'
        '<a href="mailto:someone@example.org">email</a>'
        '<a href="javascript:void(0)">js</a>'
        '</body></html>'
    )
    links = _extract_links(html, "https://www.nasa.gov/image-article/young-galaxy-cluster/")
    urls = {link["url"]: link["text"] for link in links}

    assert urls["https://esawebb.org/images/potm2606a/"] == "Read more about the image."
    # relative resolved against the base host
    assert "https://www.nasa.gov/relative/path" in urls
    # non-http schemes dropped
    assert not any(u.startswith(("mailto:", "javascript:")) for u in urls)


def test_extract_links_deduplicates():
    html = (
        '<a href="https://x.org/a">one</a>'
        '<a href="https://x.org/a">one again</a>'
        '<a href="https://x.org/b">two</a>'
    )
    links = _extract_links(html, "https://x.org/")
    assert [link["url"] for link in links] == ["https://x.org/a", "https://x.org/b"]
