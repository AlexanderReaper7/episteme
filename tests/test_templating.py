"""The markdown filter renders LLM prose as trusted HTML — it must sanitize, since
that prose is downstream of fetched web content (prompt-injectable)."""

from episteme.web.templating import _markdown


def test_markdown_renders_formatting():
    html = str(_markdown("**bold** and [a link](https://x.org/)"))
    assert "<strong>bold</strong>" in html
    assert 'href="https://x.org/"' in html


def test_markdown_strips_raw_html_and_event_handlers():
    html = str(_markdown('hi <script>alert(1)</script> <img src="x" onerror="alert(1)">'))
    assert "<script" not in html
    assert "onerror" not in html
