"""add feedback.keyword for hand-managed blocks

Revision ID: bed89c48b376
Revises: 2813f1a15b8e
Create Date: 2026-07-30 10:12:29.428787
"""

from alembic import op
import sqlalchemy as sa


revision = 'bed89c48b376'
down_revision = '2813f1a15b8e'
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# READ THIS FILE BEFORE APPLYING IT.
#
# If it came from `--autogenerate` it is a DRAFT, and drafts have known blind
# spots:
#   * A column rename is emitted as drop_column + add_column. Applying that
#     deletes every value in the column. The fix is one line:
#     op.alter_column("<table>", "<old>", new_column_name="<new>")
#   * A type change becomes a cast that runs against existing rows and may
#     truncate or fail.
#   * Data does not migrate itself -- backfills have to be written by hand.
#
# `upgrade` refuses to run while the marker below is present. Delete that line
# by hand once you have read and corrected this migration. Nothing in the
# tooling removes it for you; that deletion is the approval.
# ---------------------------------------------------------------------------
#
# Reviewed and approved (marker removed).
#
# Written by hand (`new --empty`); the three blind spots, answered:
#
# * No rename. `keyword` is a new column on `feedback`; nothing is dropped.
# * No type change. Nothing existing is cast.
# * No backfill, and NULL is correct for every existing row: `keyword` means
#   something only for kind in ('block_keyword', 'unblock_keyword'), which no row
#   can be yet, and replay skips such an event when the column is NULL rather
#   than treating it as an empty match-everything block.
#
# Deliberately NOT reusing `topic` for this. A blocked keyword is matched
# literally against a post's title and summary; a topic is slugified into the
# canonical vocabulary. Sharing one column would mean replay guessing which
# interpretation applies from `kind`, and one future miss there silently turns a
# down-rank into a content-removing block.
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.add_column("feedback", sa.Column("keyword", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("feedback", "keyword")
