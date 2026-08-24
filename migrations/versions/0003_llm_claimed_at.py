"""Track when an extraction batch claimed a row.

The stale-claim release in `maintenance` originally keyed off `fetched_at`, which is the
*ingestion* timestamp — an item ingested hours ago and claimed one second ago already looked
stale, so recovery could yank a claim out from under a running batch and re-introduce the
duplicate processing that claiming exists to prevent.

Revision ID: 0003_llm_claimed_at
Revises: 0002_llm_processing
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_llm_claimed_at"
down_revision = "0002_llm_processing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("news_items", sa.Column("llm_claimed_at", sa.DateTime(timezone=True)))
    op.execute(
        "CREATE INDEX ix_news_items_processing ON news_items (llm_claimed_at) "
        "WHERE llm_status = 'processing'"
    )


def downgrade() -> None:
    op.drop_index("ix_news_items_processing", table_name="news_items")
    op.drop_column("news_items", "llm_claimed_at")
