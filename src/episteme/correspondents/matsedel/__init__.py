"""Matsedel: the lunch menus of four restaurants in Vanersborg (0046).

The first correspondent, and the reason the class exists. It fits nothing in the
pipeline: four menus are four documents that never cluster, there is nothing to
triage because the reader has already decided lunch is worth a card, and there is
nothing to write because a menu is a list of dishes. So it reads, stores and
**files** finished posts.

Where each part lives:

- `readers.py` reads a week off a restaurant's page, politely (0005).
- `models.py` and `store.py` are the week as stored. This is where lunch history
  lives; the posts are archived and pruned, the weeks are not.
- `posts.py` turns one stored week into five weekday posts.
- `sources.py` is the four `sources` rows and the adapter that owns their type.
- `views.py` is `/c/matsedel`, its Glance block and `/c/matsedel/stats`.
- `tasks.py` is the weekly job. It is NOT imported here: it imports
  `worker.app`, and the web process has no business constructing a procrastinate
  app. The worker imports it by path (`worker/app.py:import_paths`).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from ..registry import register
from . import models as models  # noqa: F401 - imported so Alembic sees the tables
from . import views

_HERE = Path(__file__).resolve().parent


@register
class Matsedel:
    slug = "matsedel"
    label = "Matsedel"
    router: APIRouter = views.router
    stylesheet = _HERE / "static" / "matsedel.css"
    templates = _HERE / "templates"
