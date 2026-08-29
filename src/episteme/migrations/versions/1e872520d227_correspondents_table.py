"""correspondents table

The configuration half of a correspondent (0046). The code half is the plugin the
registry resolves from `slug`, exactly as `get_adapter` resolves
`Source.type_name`, and this row is what says the correspondent is configured,
what its settings are and whether it is on.

`slug` is unique because it is the identity `/c/<slug>/` is built from, and
because the registry looks a plugin up by it.

**No credential columns.** 0046 designed two, encrypted at rest with a key in
`.env`, and both belong entirely to the external-service path that the same
decision defers with no members. Two nullable columns are a cheap migration on
the day something outside this repository files a post; an encryption dependency
and a key the user has to generate, for nobody, is not.

Reviewed 2026-08-28, whole file read. Hand-written, no autogenerate involved.
A new table only: nothing to backfill, nothing to cast, no existing row touched.
`created_at` takes a server default so the column can be NOT NULL without one.

Revision ID: 1e872520d227
Revises: d4b2854cc90c
Create Date: 2026-08-28 20:40:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '1e872520d227'
down_revision = 'd4b2854cc90c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "correspondents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=50), nullable=False, unique=True),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("correspondents")
