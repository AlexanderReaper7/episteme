"""post summaries and summarized_at

Revision ID: c20374359aeb
Revises: bbe93808d5dc
Create Date: 2026-08-30 20:28:26.406850

Reviewed 2026-08-30. Two additive changes, no rename and no type change:

  * `post_summaries` holds a card summary a post USED to have (0050). Only
    superseded versions land here, so nothing is copied out of `posts` by this
    migration and there is nothing to backfill.
  * `posts.summarized_at` records when the current summary was written. NULL is
    the correct value for every existing row: none of them were written by the
    `summarize` stage, so all 683 are due for it.

WHAT THIS DOES TO THE FEED, which the diff cannot show. The feed now requires a
summary (web.app._visible_now), and 503 of the 683 published posts are aggregate
cards that have never had one - they were rendering a 240-character slice of the
source item's RSS body. Those cards leave the feed the moment this is applied and
come back as the summarize stage reaches them. The 180 article posts keep the
writer's old one-paragraph hook and stay visible throughout, replaced in place as
the stage rewrites them.

So after upgrading, run the stage over the backlog before judging the feed:

    curl -X POST "$E/api/jobs/defer/summarize"

It is ~683 fast-model calls. `GET /api/status` and the summarize count on /admin
both report what is left.
"""

from alembic import op
import sqlalchemy as sa


revision = 'c20374359aeb'
down_revision = 'bbe93808d5dc'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'post_summaries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('summarized_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'replaced_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_post_summaries_post_id'), 'post_summaries', ['post_id'], unique=False
    )
    op.add_column(
        'posts', sa.Column('summarized_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    # Dropping `post_summaries` destroys every superseded summary. That is the
    # honest downgrade: the table IS the history, and there is nowhere else to
    # put it.
    op.drop_column('posts', 'summarized_at')
    op.drop_index(op.f('ix_post_summaries_post_id'), table_name='post_summaries')
    op.drop_table('post_summaries')
