"""posts href and the body-iff-self-rendering check

A post stores where its content lives (0047). `href` NULL means the post renders
itself at /post/{id}; an aggregate holds its primary item's URL, so a card and
the canonical URL both go straight to the source instead of to a page that
relisted the cluster the card had already shown.

Three statements in an order that matters:

1. Add `posts.href`, nullable. Every existing row starts NULL.
2. Backfill every aggregate from its primary item: earliest `published_at`, then
   lowest `id`, the same ordering `models.PRIMARY_ITEM_ORDER` gives `Story.items`.
   This is the part `--autogenerate` cannot see, which is why the file is written
   by hand.
3. Only then add `ck_posts_body_iff_self_rendering`. Before the backfill every
   aggregate would violate it, since `sections` is `[]` and `href` would still be
   NULL.

The `jsonb_typeof` guard in the CHECK is load-bearing: `jsonb_array_length`
raises on a JSON scalar instead of returning false, so without it a stray
`'null'::jsonb` would make the constraint ERROR rather than reject, and an
erroring constraint is harder to diagnose than a failing one.

Reviewed 2026-08-28, whole file read. Hand-written, no autogenerate involved.
Measured against the live database before writing it: `sections` is NOT NULL and
a JSON array in all 646 rows; all 473 aggregates have a body-less `sections`
of `[]` and all 173 articles have a non-empty one, so the constraint describes
the data as it already stands. No aggregate has an item-less story, so the
backfill cannot leave a NULL for the constraint to reject; the guard below says
so out loud rather than trusting the measurement to still hold at apply time.

12 of the 473 aggregate cards change which item they lead with, because
`Story.items` had no `order_by` at all and was returning physical row order. That
is a one-time correction of cards that were already choosing arbitrarily.

Revision ID: 499afada00fa
Revises: 192537060e8f
Create Date: 2026-08-28 21:52:29.147439
"""

import sqlalchemy as sa
from alembic import op

revision = "499afada00fa"
down_revision = "192537060e8f"
branch_labels = None
depends_on = None

CHECK_NAME = "ck_posts_body_iff_self_rendering"
CHECK_SQL = (
    "(href IS NULL) = (jsonb_typeof(sections) = 'array' AND jsonb_array_length(sections) > 0)"
)


def upgrade() -> None:
    op.add_column("posts", sa.Column("href", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE posts p
           SET href = (
                 SELECT si.url
                   FROM source_items si
                  WHERE si.story_id = p.story_id
                  ORDER BY si.published_at ASC NULLS LAST, si.id ASC
                  LIMIT 1
               )
         WHERE p.kind = 'aggregate'
        """
    )
    # Name the rows rather than letting the constraint fail with only its own
    # name: an aggregate whose story lost its items would come out of the
    # backfill still NULL, and "which posts" is the first thing anyone asks.
    stranded = (
        op.get_bind()
        .execute(sa.text(f"SELECT id FROM posts WHERE NOT ({CHECK_SQL}) ORDER BY id"))
        .scalars()
        .all()
    )
    if stranded:
        raise RuntimeError(
            f"{len(stranded)} post(s) carry neither a body nor a destination, or "
            f"both: {stranded[:20]}{' ...' if len(stranded) > 20 else ''}. "
            "An aggregate with an item-less story is the expected cause; give it "
            "an href or delete it, then re-run."
        )
    op.create_check_constraint(CHECK_NAME, "posts", CHECK_SQL)


def downgrade() -> None:
    op.drop_constraint(CHECK_NAME, "posts", type_="check")
    op.drop_column("posts", "href")
