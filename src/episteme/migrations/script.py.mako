"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

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
UNREVIEWED = True


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
