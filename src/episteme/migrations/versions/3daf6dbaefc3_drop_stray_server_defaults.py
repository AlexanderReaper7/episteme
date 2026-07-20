"""drop stray server defaults

Three columns carry a Postgres-level DEFAULT that models.py never declared:
posts.kind, posts.pinned and llm_calls.pinned. They are an artifact of how those
columns were added under the old bootstrap.py DDL lists -- adding a NOT NULL
column to a populated table needs a DEFAULT so existing rows get a value, and the
clause was simply left in place afterwards.

Every other scalar default in models.py is Python-side (`mapped_column(default=...)`,
supplied by SQLAlchemy on insert); the only `server_default`s there are `func.now()`
timestamps. So these three are drift, not design, and while they persist every
autogenerate run re-proposes them as a diff.

Dropping a default changes no rows -- it only stops Postgres filling the column in
on an INSERT that omits it. Nothing does that: the sole raw INSERT into posts lived
in bootstrap.py's migration list (deleted in this same change) and named `kind`
explicitly. Everything else goes through the ORM, which supplies the Python default.

Safe on a fresh database too: the baseline creates these columns with no default,
and ALTER COLUMN ... DROP DEFAULT is a no-op when there is nothing to drop.

Revision ID: 3daf6dbaefc3
Revises: 794b362d6e01
Create Date: 2026-07-20 13:23:32.023383
"""

import sqlalchemy as sa
from alembic import op

revision = '3daf6dbaefc3'
down_revision = '794b362d6e01'
branch_labels = None
depends_on = None

# Reviewed 2026-07-20: hand-written, no autogenerate involved. Verified against
# the live schema diff that produced it (3 modify_default entries, nothing else).


def upgrade() -> None:
    op.alter_column(
        "posts", "kind", server_default=None,
        existing_type=sa.String(length=20), existing_nullable=False,
    )
    op.alter_column(
        "posts", "pinned", server_default=None,
        existing_type=sa.Boolean(), existing_nullable=False,
    )
    op.alter_column(
        "llm_calls", "pinned", server_default=None,
        existing_type=sa.Boolean(), existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "posts", "kind", server_default=sa.text("'feature'::character varying"),
        existing_type=sa.String(length=20), existing_nullable=False,
    )
    op.alter_column(
        "posts", "pinned", server_default=sa.text("false"),
        existing_type=sa.Boolean(), existing_nullable=False,
    )
    op.alter_column(
        "llm_calls", "pinned", server_default=sa.text("false"),
        existing_type=sa.Boolean(), existing_nullable=False,
    )
