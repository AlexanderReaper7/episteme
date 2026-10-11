"""A Glance block as data: the optional JSON contract (0062).

`GET /c/<slug>/glance` is the HTML block every correspondent must serve (0046).
`GET /c/<slug>/glance.json` is the same block as a structure the Android app's
launcher panel draws natively. It is optional: a correspondent without it is
drawn by a WebView of `/glance/<slug>`, its HTML block alone.

The block knows nothing of any one correspondent. A heading with an optional
link, then named groups of lines, where a line is either a label or content.
A plugin that serves both should build its block once and render the HTML from
the same build, so the two cannot say different things.
"""

from __future__ import annotations

from pydantic import BaseModel

#: Optional, unlike `registry.GLANCE_PATH`.
GLANCE_JSON_PATH = "/glance.json"


class GlanceLine(BaseModel):
    text: str
    #: A heading inside the group, such as `Från buffé:`, not content.
    label: bool = False


class GlanceGroup(BaseModel):
    name: str
    lines: list[GlanceLine]


class GlanceBlock(BaseModel):
    heading: str | None = None
    #: Where the heading goes, a path on this server.
    href: str | None = None
    groups: list[GlanceGroup] = []
    #: The sentence shown when `groups` is empty. An empty block must read as a
    #: correspondent with nothing to say, never as one that is down (0046).
    empty: str | None = None


def serves_json(plugin) -> bool:
    """Whether a plugin's router declares the JSON block. `None` is an external
    correspondent with no plugin here, which serves HTML only."""
    if plugin is None:
        return False
    return any(
        route.path == GLANCE_JSON_PATH and "GET" in (getattr(route, "methods", None) or ())
        for route in plugin.router.routes
    )
