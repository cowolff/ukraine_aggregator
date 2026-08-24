"""Add the 'processing' llm_status so extraction batches can claim rows.

Without a claim step, concurrent `llm_extract_batch` workers all select the same pending rows
(`SELECT ... LIMIT` takes no locks), extract them repeatedly and write duplicate events. Measured
under a 6-way concurrent load test: 314 news items ended up with duplicated event groups.

Revision ID: 0002_llm_processing
Revises: 0001_initial
"""
from __future__ import annotations

from alembic import op

revision = "0002_llm_processing"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

OLD = ("pending", "done", "skipped", "failed")
NEW = ("pending", "processing", "done", "skipped", "failed")


def upgrade() -> None:
    op.drop_constraint("llm_status", "news_items", type_="check")
    op.create_check_constraint("llm_status", "news_items", f"llm_status IN {NEW!r}")


def downgrade() -> None:
    op.execute("UPDATE news_items SET llm_status = 'pending' WHERE llm_status = 'processing'")
    op.drop_constraint("llm_status", "news_items", type_="check")
    op.create_check_constraint("llm_status", "news_items", f"llm_status IN {OLD!r}")
