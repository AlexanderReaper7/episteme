"""Which correspondent a slug means, and what it brought with it (0046).

`ingest/registry.py` is the model: a decorator that instantiates a class and
files it under a name, and a lookup that raises with the known names rather than
a bare `KeyError`. The difference is what a correspondent registers. An adapter
brings two methods; a correspondent brings a router, and optionally a stylesheet.

**Core owns the prefix.** A plugin's router carries paths relative to its own
page (`""`, `"/stats"`), and `web/correspondents.py` mounts it under
`/c/<slug>`. A plugin cannot choose where it lands, so it cannot shadow a core
route by declaring one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from fastapi import APIRouter


#: Every correspondent has a Glance block (0046), so core fetches this path from
#: every one of them. Required at registration rather than discovered at render
#: time, where a missing route is indistinguishable from a correspondent that is
#: down - which is the one thing Glance must not confuse.
GLANCE_PATH = "/glance"


class RegistryError(Exception):
    """A plugin that could not be registered as declared."""


@runtime_checkable
class CorrespondentPlugin(Protocol):
    """What a correspondent's package hands over.

    The router must declare `GET /glance`, which is the block core puts on the
    Glance page. `slug` is the permanent identity and must match the
    `correspondents` row;
    `label` is what a page prints and may be renamed at will. `stylesheet` is an
    absolute path to an additive CSS file, served by core at
    `/c/<slug>/style.css` and loaded only on the correspondent's own pages and on
    Glance, never on the feed. Both optional attributes may simply be `None`.
    """

    slug: str
    label: str
    router: APIRouter
    stylesheet: Path | None
    #: Directory of Jinja templates, searched after core's so nothing can shadow
    #: `base.html`. A page template extends `correspondent.html`, which emits the
    #: scoping wrapper and the stylesheet link.
    templates: Path | None


_PLUGINS: dict[str, CorrespondentPlugin] = {}


def register(plugin_cls: type) -> type:
    """Class decorator: instantiate and register a correspondent by its slug."""
    plugin = plugin_cls()
    validate(plugin)
    if plugin.slug in _PLUGINS:
        raise RegistryError(f"two plugins claim the slug {plugin.slug!r}")
    _PLUGINS[plugin.slug] = plugin
    return plugin_cls


def validate(plugin: CorrespondentPlugin) -> None:
    """Everything that has to be true before a plugin is mounted.

    Runs at import, so a malformed plugin is a startup failure rather than a 404
    nobody can explain. The slug check is the load-bearing one: it becomes a URL
    prefix, and a slug with a slash in it would mount routes somewhere core never
    intended.
    """
    slug = getattr(plugin, "slug", None)
    if not slug or not slug.replace("-", "").isalnum() or not slug.islower():
        raise RegistryError(
            f"correspondent slug {slug!r} must be lowercase alphanumeric with dashes: "
            "it is a URL path segment"
        )
    if not getattr(plugin, "label", None):
        raise RegistryError(f"{slug}: no label, and the label is what a page prints")
    if not isinstance(getattr(plugin, "router", None), APIRouter):
        raise RegistryError(f"{slug}: no APIRouter; a correspondent owns a page")
    sheet = getattr(plugin, "stylesheet", None)
    if sheet is not None and not Path(sheet).is_file():
        raise RegistryError(f"{slug}: stylesheet {sheet} does not exist")
    tpl = getattr(plugin, "templates", None)
    if tpl is not None and not Path(tpl).is_dir():
        raise RegistryError(f"{slug}: template directory {tpl} does not exist")
    router = plugin.router
    served = {
        route.path for route in router.routes if "GET" in (getattr(route, "methods", None) or ())
    }
    if GLANCE_PATH not in served:
        raise RegistryError(
            f"{slug}: no GET {GLANCE_PATH} route. Every correspondent has a block "
            f"on Glance (0046); it renders one summary of what is standing now."
        )


def get_plugin(slug: str) -> CorrespondentPlugin:
    try:
        return _PLUGINS[slug]
    except KeyError:
        raise LookupError(
            f"No correspondent plugin registered for {slug!r}; known: {sorted(_PLUGINS)}"
        ) from None


def plugins() -> list[CorrespondentPlugin]:
    """Every registered plugin, slug order. Mounting and Glance both walk this."""
    return [_PLUGINS[slug] for slug in sorted(_PLUGINS)]
