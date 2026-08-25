"""Keyset-ordering index for /api/news?order=published.

The news rail sorts by reporting time rather than ingest order (plans/NEWS_RAIL.md addendum):
`COALESCE(published_at, fetched_at) DESC, id DESC`. The existing ix_news_items_published_at
cannot serve that — it indexes the bare column, and NULL published_at rows (which fall back to
fetched_at) would sort wrongly — so the expression itself is indexed, with id as the tiebreaker
the keyset cursor compares on.

Revision ID: 0008_news_published_order
Revises: 0007_geocode_metadata
"""
from __future__ import annotations

from alembic import op

revision = "0008_news_published_order"
down_revision = "0007_geocode_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_news_items_effective_published ON news_items "
        "((COALESCE(published_at, fetched_at)) DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_news_items_effective_published")
