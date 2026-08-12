# Importing adapter modules registers them (see registry.register).
from . import manual, rss  # noqa: F401
from .base import ExtractedItem, RawItem, SourceAdapter, canonicalize_url, content_hash
from .registry import get_adapter, register

__all__ = [
    "ExtractedItem",
    "RawItem",
    "SourceAdapter",
    "canonicalize_url",
    "content_hash",
    "get_adapter",
    "register",
]
