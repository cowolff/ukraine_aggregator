"""English translations for public display.

Most of the corpus is Ukrainian or Russian. The public site shows headlines, so an English
rendering is stored alongside the original rather than replacing it — the original stays
authoritative and the UI can toggle back to it.

Revision ID: 0005_translations
Revises: 0004_time_travel
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_translations"
down_revision = "0004_time_travel"
branch_labels = None
depends_on = None

STATUSES = ("pending", "processing", "done", "skipped", "failed")


def upgrade() -> None:
    op.add_column("news_items", sa.Column("title_en", sa.Text()))
    op.add_column("news_items", sa.Column("body_en", sa.Text()))
    op.add_column(
        "news_items",
        sa.Column(
            "translation_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")
        ),
    )
    op.add_column("news_items", sa.Column("translation_claimed_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "translation_status", "news_items", f"translation_status IN {STATUSES!r}"
    )
    # The translator's work queue: newest-first over items still needing a pass.
    op.execute(
        "CREATE INDEX ix_news_items_translation_pending ON news_items (id DESC) "
        "WHERE translation_status IN ('pending', 'failed')"
    )
    op.execute(
        "CREATE INDEX ix_news_items_translation_processing ON news_items (translation_claimed_at) "
        "WHERE translation_status = 'processing'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_news_items_translation_processing")
    op.execute("DROP INDEX IF EXISTS ix_news_items_translation_pending")
    op.drop_constraint("translation_status", "news_items", type_="check")
    for column in ("translation_claimed_at", "translation_status", "body_en", "title_en"):
        op.drop_column("news_items", column)
