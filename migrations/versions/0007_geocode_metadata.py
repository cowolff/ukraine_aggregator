"""Geocode resolution metadata.

`extracted_events.geo_meta` records how a point was placed (hinted / countrywide / fallback /
manual, plus the similarity score and the raw LLM oblast hint), so mislocation audits are a
single query instead of log archaeology.

Revision ID: 0007_geocode_metadata
Revises: 0006_news_placement_index
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_geocode_metadata"
down_revision = "0006_news_placement_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extracted_events",
        sa.Column("geo_meta", sa.dialects.postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("extracted_events", "geo_meta")
