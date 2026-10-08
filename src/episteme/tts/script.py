"""Build the spoken narration script from a post.

Deterministic, so the same post always yields the same script (and the same
`script_hash`, which the narrate stage uses to skip unchanged posts). This is the
seam the future LLM preprocessing pass replaces: it will emit a marked-up script
(emotion cues, pronunciation) in place of this plain text — the provider that
consumes the string does not change.

Only the sections that carry spoken meaning are read. Visual/interactive sections
(image, video, chart, diagram) contribute their caption when they have one; quiz
sections are skipped entirely (they make no sense read aloud).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


def script_hash(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def _markdown_to_speech(md: str) -> str:
    """Strip Markdown down to plain prose a TTS engine reads cleanly. Not a full
    parser — just enough that syntax characters aren't spoken: links become their
    text, emphasis/heading/list/quote markers and inline code fences are removed,
    and whitespace is collapsed."""
    text = md.replace("\r\n", "\n")
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)  # fenced code blocks
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # links -> link text
    text = re.sub(r"`([^`]*)`", r"\1", text)  # inline code
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)  # bold/italic
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)  # headings
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)  # blockquotes
    text = re.sub(r"^\s{0,3}[-*+]\s+", "", text, flags=re.MULTILINE)  # bullets
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip()


def _ensure_sentence(text: str) -> str:
    """Give a fragment terminal punctuation so the engine pauses between items."""
    text = text.strip()
    if text and text[-1] not in ".!?:;":
        text += "."
    return text


def _section_speech(section: dict[str, Any]) -> str:
    """Spoken text for one section, or "" if it contributes nothing audible."""
    kind = section.get("type")
    if kind == "prose":
        return _markdown_to_speech(section.get("text", ""))
    if kind == "key_points":
        return "\n".join(
            _ensure_sentence(item) for item in section.get("items", []) if item.strip()
        )
    if kind == "glossary":
        return "\n".join(
            _ensure_sentence(f"{t.get('term', '')}: {t.get('definition', '')}")
            for t in section.get("terms", [])
        )
    if kind == "timeline":
        return "\n".join(
            _ensure_sentence(f"{e.get('date', '')}: {e.get('label', '')}")
            for e in section.get("events", [])
        )
    if kind in ("image", "video", "chart", "diagram"):
        # Read the caption so a listener knows a visual was here; skip the asset.
        caption = (section.get("caption") or "").strip()
        return _ensure_sentence(caption) if caption else ""
    # quiz and anything unknown: nothing to read aloud.
    return ""


def build_script(post: Any) -> str:
    """Assemble the full narration script for an article post: title, summary, then
    each section's spoken text in order. `post` may be a Post ORM row or any object
    exposing `title`, `summary`, and `sections` (a list of section dicts)."""
    parts: list[str] = []
    if post.title:
        parts.append(_ensure_sentence(post.title.strip()))
    if post.summary:
        parts.append(_markdown_to_speech(post.summary))
    for section in post.sections or []:
        spoken = _section_speech(section).strip()
        if spoken:
            parts.append(spoken)
    return "\n\n".join(p for p in parts if p)
