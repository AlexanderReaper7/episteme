"""The markdown filter renders LLM prose as trusted HTML — it must sanitize, since
that prose is downstream of fetched web content (prompt-injectable). The
video_embed filter is the iframe whitelist: only known players ever embed."""

import pytest

from episteme.web.templating import _markdown, _video_embed


def test_markdown_renders_formatting():
    html = str(_markdown("**bold** and [a link](https://x.org/)"))
    assert "<strong>bold</strong>" in html
    assert 'href="https://x.org/"' in html


def test_markdown_strips_raw_html_and_event_handlers():
    html = str(_markdown('hi <script>alert(1)</script> <img src="x" onerror="alert(1)">'))
    assert "<script" not in html
    assert "onerror" not in html


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
        ),
        ("https://youtu.be/dQw4w9WgXcQ", "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"),
        (
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
        ),
        ("https://player.vimeo.com/video/76979871", "https://player.vimeo.com/video/76979871"),
        ("https://vimeo.com/76979871", "https://player.vimeo.com/video/76979871"),
    ],
)
def test_video_embed_normalizes_known_players(url, expected):
    assert _video_embed(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/embed/xss",
        "https://evil.example/?u=https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "javascript:alert(1)",
        "",
        None,
    ],
)
def test_video_embed_rejects_everything_else(url):
    assert _video_embed(url) is None
