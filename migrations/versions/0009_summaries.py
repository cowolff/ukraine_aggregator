"""English summaries for display.

Each item gets a general English summary (news_items.summary_en). Items pinned to the map
additionally get a location-focused summary per distinct place name, stored on the extracted
events (extracted_events.summary_en) so every marker of a multi-location item explains what is
reported at *its* spot. Generated after extraction so the place names are known.

Revision ID: 0009_summaries
Revises: 0008_news_published_order
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_summaries"
down_revision = "0008_news_published_order"
branch_labels = None
depends_on = None

STATUSES = ("pending", "processing", "done", "skipped", "failed")


def upgrade() -> None:
    op.add_column("news_items", sa.Column("summary_en", sa.Text()))
    op.add_column(
        "news_items",
        sa.Column("summary_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
    )
    op.add_column("news_items", sa.Column("summary_claimed_at", sa.DateTime(timezone=True)))
    op.create_check_constraint("summary_status", "news_items", f"summary_status IN {STATUSES!r}")
    # The summariser's work queue: newest-first over items still needing a pass.
    op.execute(
        "CREATE INDEX ix_news_items_summary_pending ON news_items (id DESC) "
        "WHERE summary_status IN ('pending', 'failed')"
    )
    op.execute(
        "CREATE INDEX ix_news_items_summary_processing ON news_items (summary_claimed_at) "
        "WHERE summary_status = 'processing'"
    )
    op.add_column("extracted_events", sa.Column("summary_en", sa.Text()))


def downgrade() -> None:
    op.drop_column("extracted_events", "summary_en")
    op.execute("DROP INDEX IF EXISTS ix_news_items_summary_processing")
    op.execute("DROP INDEX IF EXISTS ix_news_items_summary_pending")
    op.drop_constraint("summary_status", "news_items", type_="check")
    for column in ("summary_claimed_at", "summary_status", "summary_en"):
        op.drop_column("news_items", column)
