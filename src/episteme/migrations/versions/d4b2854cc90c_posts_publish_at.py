"""posts publish at

When a post becomes visible (0046). NULL means immediately, which is every row
that exists today and every post the writer and the clusterer will ever make.

The column exists so a correspondent can read a week of content once and create
all five weekday posts from that single read, each carrying its own date. The
alternative was minting each day's post during the 03:00 pipeline run, which
would have made a lunch menu depend on a nightly LLM run it needs nothing from:
no card on any night the pipeline is paused, the governor holds the GPU, or a job
failed.

One column, no job, no extra status, nothing that can fail to fire. The feed
filters on `publish_at IS NULL OR publish_at <= now()` and sorts on it.

Reviewed 2026-08-28, whole file read. Hand-written, no autogenerate involved. A
plain nullable timestamptz on a table of 646 rows: no backfill, no default, no
cast against existing data, and nothing that reads the column until a producer
sets one.

Revision ID: d4b2854cc90c
Revises: 499afada00fa
Create Date: 2026-08-28 20:22:11.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "d4b2854cc90c"
down_revision = "499afada00fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("publish_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "publish_at")
