"""Alembic migrations, shipped inside the package.

Living under `src/episteme/` rather than at the repo root is deliberate: the
Dockerfile copies only `src`, so this is what puts the revision scripts and the
review ledger into the image, and `script_location` then resolves identically
from an editable dev install and from site-packages in the container.

The database URL is *not* stored in alembic.ini — it comes from `settings` at
runtime (env.py), which keeps one source of truth and dodges ConfigParser's `%`
interpolation mangling passwords.
"""

from pathlib import Path

from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent
VERSIONS_DIR = MIGRATIONS_DIR / "versions"


def alembic_config() -> Config:
    """A Config wired to this package, usable from anywhere (bootstrap, CLI,
    tests) without depending on the process's working directory."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_locations", str(VERSIONS_DIR))
    # Windows paths contain ':' and spaces; the legacy splitter would tear them
    # apart. 'os' means "use this platform's path separator".
    config.set_main_option("path_separator", "os")
    # Revision filenames lead with a zero-padded counter so `ls` shows history
    # order; alembic still tracks the real order through down_revision.
    config.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    return config
