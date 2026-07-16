from datetime import UTC, datetime

from episteme.ingest.base import canonicalize_url, content_hash
from episteme.ingest.rss import parse_feed

SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Older discovery</title>
      <link>https://example.org/articles/old</link>
      <pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate>
      <description>An older item.</description>
    </item>
    <item>
      <title>New quantum result</title>
      <link>https://example.org/articles/quantum?utm_source=rss</link>
      <author>a.researcher@example.org</author>
      <pubDate>Wed, 01 Jan 2025 12:30:00 GMT</pubDate>
      <description>Something new about qubits.</description>
      <media:content url="https://example.org/img/quantum.jpg" medium="image"/>
    </item>
  </channel>
</rss>
"""


def test_parse_feed_maps_entries():
    items = parse_feed(SAMPLE_RSS)
    assert len(items) == 2
    newest = next(i for i in items if "quantum" in i.url)
    assert newest.title == "New quantum result"
    assert newest.published_at == datetime(2025, 1, 1, 12, 30, tzinfo=UTC)
    assert newest.summary == "Something new about qubits."
    assert newest.media_refs == [{"kind": "image", "url": "https://example.org/img/quantum.jpg"}]


def test_parse_feed_since_filter():
    since = datetime(2024, 6, 1, tzinfo=UTC)
    items = parse_feed(SAMPLE_RSS, since=since)
    assert [i.title for i in items] == ["New quantum result"]


def test_canonicalize_url_strips_tracking_and_fragment():
    url = "HTTPS://Example.org/a/b?utm_source=rss&id=7&fbclid=x#section"
    assert canonicalize_url(url) == "https://example.org/a/b?id=7"


def test_content_hash_ignores_tracking_params():
    assert content_hash("https://example.org/a?utm_source=x") == content_hash(
        "https://example.org/a"
    )
