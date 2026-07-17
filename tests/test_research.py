"""Research tools: the SSRF guard (the untrusted-fetch security boundary) and
outbound-link extraction (how the agent finds a 'read more' link)."""

import pytest

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


def test_validate_url_allows_public_ip():
    assert _validate_url("http://8.8.8.8/") == "http://8.8.8.8/"


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
