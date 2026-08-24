"""Time axis: when things happened, as distinct from when we ingested them.

Everything so far has been filtered on ingest time (`extracted_events.created_at`,
`frontline_snapshots.built_at`). That is fine for a live map but useless for going back in time:
a snapshot backfilled today for April 2022 was *built* today. Each row therefore gets an explicit
"what instant does this describe" column:

* ``extracted_events.occurred_at``     — when the reported event happened (the item's publication
                                         time), not when the poller saw it.
* ``frontline_snapshots.valid_at``     — the instant this geometry depicts.
* ``upstream_geometries.valid_at``     — same, for the raw upstream layers.

Revision ID: 0004_time_travel
Revises: 0003_llm_claimed_at
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_time_travel"
down_revision = "0003_llm_claimed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A GIST index mixing a geometry with a timestamptz needs btree_gist to supply the operator
    # class for the scalar column. It is what makes "in this bbox during this window" — the hot
    # query once the map can scrub through time — a single index scan.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.add_column(
        "extracted_events",
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing rows: the item's publication time is the best available answer, falling back to
    # when it was fetched.
    op.execute(
        """
        UPDATE extracted_events e
        SET occurred_at = COALESCE(n.published_at, n.fetched_at, e.created_at)
        FROM news_items n WHERE n.id = e.news_item_id
        """
    )
    op.execute("UPDATE extracted_events SET occurred_at = created_at WHERE occurred_at IS NULL")
    op.alter_column("extracted_events", "occurred_at", nullable=False)
    op.execute(
        "ALTER TABLE extracted_events ALTER COLUMN occurred_at SET DEFAULT now()"
    )
    op.execute("CREATE INDEX ix_extracted_events_occurred ON extracted_events (occurred_at DESC)")
    # The map's hot query is "placed, visible events in this bbox during this window".
    op.execute(
        "CREATE INDEX ix_extracted_events_occurred_geom ON extracted_events "
        "USING gist (geom, occurred_at) WHERE geom IS NOT NULL"
    )

    for table, source in (("frontline_snapshots", "built_at"), ("upstream_geometries", "fetched_at")):
        op.add_column(table, sa.Column("valid_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(f"UPDATE {table} SET valid_at = {source} WHERE valid_at IS NULL")
        op.alter_column(table, "valid_at", nullable=False)
        op.execute(f"ALTER TABLE {table} ALTER COLUMN valid_at SET DEFAULT now()")

    # Time-travel lookup: newest snapshot at or before an instant, per layer.
    op.execute(
        "CREATE INDEX ix_frontline_snapshots_layer_valid ON frontline_snapshots (layer, valid_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_upstream_geometries_provider_valid "
        "ON upstream_geometries (provider, valid_at DESC)"
    )
    # A backfill must be re-runnable without duplicating history.
    op.execute(
        "CREATE UNIQUE INDEX uq_upstream_geometries_provider_version "
        "ON upstream_geometries (provider, upstream_version) WHERE upstream_version IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_frontline_snapshots_layer_valid "
        "ON frontline_snapshots (layer, valid_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_frontline_snapshots_layer_valid")
    op.execute("DROP INDEX IF EXISTS uq_upstream_geometries_provider_version")
    op.execute("DROP INDEX IF EXISTS ix_upstream_geometries_provider_valid")
    op.execute("DROP INDEX IF EXISTS ix_frontline_snapshots_layer_valid")
    op.execute("DROP INDEX IF EXISTS ix_extracted_events_occurred_geom")
    op.execute("DROP INDEX IF EXISTS ix_extracted_events_occurred")
    op.drop_column("upstream_geometries", "valid_at")
    op.drop_column("frontline_snapshots", "valid_at")
    op.drop_column("extracted_events", "occurred_at")
