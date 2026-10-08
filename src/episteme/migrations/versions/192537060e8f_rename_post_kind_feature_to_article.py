"""rename post kind feature to article

The long-form kind was called `feature`, which collides with the ordinary
software sense of the word: `config.py` used both meanings 234 lines apart, and
`style.css` declared `--card-feature-border` with the comment "generated-article
card accent border". The code already said "article" everywhere except the enum
(163 occurrences against 140), so this makes the stored value agree with the
prose, the CSS classes and the `write_article_from_url` tool. See 0045.

Data only. `posts.kind` is a plain `String(20)` with no enum type, no check
constraint and no server default (3daf6dbaefc3 dropped the last one), so nothing
but the rows themselves has to change. Earlier migrations that match on
`kind = 'feature'` are left alone: they record what ran against the schema of
their day, and rewriting them would make history disagree with itself.

Reviewed 2026-08-28: hand-written, no autogenerate involved. Verified that
`kind` carries only 'feature' and 'aggregate' before this runs; the WHERE clause
leaves aggregate rows untouched, and the statement is idempotent.

Revision ID: 192537060e8f
Revises: 05618d4b1026
Create Date: 2026-08-28 13:21:37.354357
"""

from alembic import op

revision = "192537060e8f"
down_revision = "05618d4b1026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE posts SET kind = 'article' WHERE kind = 'feature'")


def downgrade() -> None:
    op.execute("UPDATE posts SET kind = 'feature' WHERE kind = 'article'")
