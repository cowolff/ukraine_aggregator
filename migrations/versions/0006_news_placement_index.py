"""Placement probe index for the news rail.

`/api/news?placement=…` (plans/NEWS_RAIL.md) classifies every item by an (anti-)semi-join on
"has at least one visible extracted event with a geometry". This partial index keys that probe
by `news_item_id` so the check is an index lookup per candidate row instead of a scan.

Revision ID: 0006_news_placement_index
Revises: 0005_translations
"""
from __future__ import annotations

from alembic import op

revision = "0006_news_placement_index"
down_revision = "0005_translations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_extracted_events_item_placed ON extracted_events (news_item_id) "
        "WHERE visible AND geom IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_extracted_events_item_placed")
