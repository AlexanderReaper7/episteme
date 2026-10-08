"""The `correspondents` rows behind the registered plugins (0046).

A row is the configuration, a plugin is the code, and neither implies the other.
Installing a plugin is the decision to run it here, so a plugin with no row gets
one on bootstrap, enabled, the way `manual.manual_source` creates its source row
on first use. Seeding proper is not the mechanism: `seeds.py` only fires into an
empty table, so every database that already exists would never get a row.

A row whose plugin has been removed is left alone. It is a record of something
that ran, its `config` may be the only surviving copy of how it was set up, and
deleting it on the strength of an import having disappeared is not a decision
this function is entitled to make.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Correspondent
from .registry import plugins

log = logging.getLogger("episteme.correspondents.rows")


async def ensure_rows(session: AsyncSession) -> int:
    """Get-or-create one row per registered plugin. Returns how many were added."""
    known = {row.slug: row for row in (await session.execute(select(Correspondent))).scalars()}
    added = 0
    for plugin in plugins():
        row = known.get(plugin.slug)
        if row is None:
            session.add(Correspondent(slug=plugin.slug, label=plugin.label, config={}))
            added += 1
            log.info("Registered correspondent %r", plugin.slug)
        elif row.label != plugin.label:
            # The plugin owns its own name; the row carries it so `/admin` and
            # Glance can read a label without importing anything.
            row.label = plugin.label
    if added or session.dirty:
        await session.commit()
    return added


async def enabled_slugs(session: AsyncSession) -> set[str]:
    return set(
        (
            await session.execute(select(Correspondent.slug).where(Correspondent.enabled.is_(True)))
        ).scalars()
    )
