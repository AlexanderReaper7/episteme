"""add post_audio table

Revision ID: 9f854ae4067e
Revises: 3daf6dbaefc3
Create Date: 2026-07-21 13:08:56.444199
"""

from alembic import op
import sqlalchemy as sa


revision = "9f854ae4067e"
down_revision = "3daf6dbaefc3"
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
#
# Reviewed 2026-07-21: pure CREATE TABLE for the new post_audio (per-voice
# narration metadata, unique on (post_id, voice)). No renames, no type changes, no
# data to backfill; the posts.id FK carries ondelete=CASCADE so the retention
# prune's bulk post delete cascades cleanly. Safe to apply.
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.create_table(
        "post_audio",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("voice", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("audio_format", sa.String(length=10), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("script_hash", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id", "voice", name="uq_post_audio_post_voice"),
    )
    op.create_index("ix_post_audio_post_id", "post_audio", ["post_id"])


def downgrade() -> None:
    op.drop_index("ix_post_audio_post_id", table_name="post_audio")
    op.drop_table("post_audio")
