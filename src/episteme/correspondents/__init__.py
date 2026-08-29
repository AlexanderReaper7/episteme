"""Correspondents: producers of finished content that file it (0046).

A correspondent skips triage and the writer entirely. It hands over a post that
is already what the reader will see, and Episteme gives it identity, storage and
its place in the feed. How it got its content is its own business.

`filing` is the entry point, `registry` resolves a slug to the plugin that owns
it, and `rows` is the configuration half. The routes under `/c/<slug>/` are
mounted by `web/correspondents.py` and laid out on Glance by `web/glance.py`,
because core owns the prefix and the page.

Plugins are imported here, the way `ingest/__init__` imports its adapters: the
registry is populated by import, so anything that has not been imported does not
exist. `migrations/env.py` imports this module for the same reason - a plugin's
tables are in core's single Alembic history, and autogenerate cannot see a table
whose class was never imported.
"""

from . import matsedel as matsedel  # noqa: F401 - registers the plugin
from .filing import FiledItem, FilingError, file_post
from .registry import CorrespondentPlugin, RegistryError, get_plugin, plugins, register
from .rows import ensure_rows

__all__ = [
    "CorrespondentPlugin",
    "FiledItem",
    "FilingError",
    "RegistryError",
    "ensure_rows",
    "file_post",
    "get_plugin",
    "plugins",
    "register",
]
