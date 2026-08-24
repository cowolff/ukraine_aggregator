"""Initial schema: sources, news, events, gazetteer, claims, snapshots, admin (PLAN §7).

Revision ID: 0001_initial
Revises:
"""
from __future__ import annotations

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

PERSPECTIVES = ("ukrainian", "russian", "western", "neutral")
SOURCE_TYPES = ("rss", "telegram", "api", "scrape")
SOURCE_STATUSES = ("ok", "degraded", "dead")
LLM_STATUSES = ("pending", "done", "skipped", "failed")
EVENT_TYPES = (
    "frontline_advance", "frontline_claim", "deep_strike", "shelling",
    "geolocation_proof", "debunk", "other",
)
CLAIM_DIRECTIONS = ("ru_advance", "ua_advance")
CLAIM_STATUSES = ("pending", "confirmed", "rejected", "reverted")
EVIDENCE_ROLES = ("support", "geolocation_proof", "debunk")
SNAPSHOT_LAYERS = ("ru", "grey")
NOTIFICATION_LEVELS = ("info", "warning", "alert")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN {values!r}"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("perspective", sa.Text(), nullable=False),
        sa.Column("reliability_tier", sa.SmallInteger(), nullable=False, server_default=sa.text("2")),
        sa.Column("poll_interval_s", sa.Integer(), nullable=False, server_default=sa.text("900")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("etag", sa.Text()),
        sa.Column("last_modified", sa.Text()),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'ok'")),
        sa.Column("meta", sa.dialects.postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.CheckConstraint(_in("type", SOURCE_TYPES), name="type"),
        sa.CheckConstraint(_in("perspective", PERSPECTIVES), name="perspective"),
        sa.CheckConstraint(_in("status", SOURCE_STATUSES), name="status"),
    )
    op.create_index("ix_sources_due", "sources", ["enabled", "last_polled_at"])

    op.create_table(
        "gazetteer",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("katottg", sa.Text()),
        sa.Column("name_uk", sa.Text(), nullable=False),
        sa.Column("name_ru", sa.Text()),
        sa.Column("name_en", sa.Text()),
        sa.Column("name_search", sa.Text(), nullable=False),
        sa.Column("oblast", sa.Text()),
        sa.Column("raion", sa.Text()),
        sa.Column("population", sa.Integer()),
        sa.Column("geom", geoalchemy2.Geometry("POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("boundary", geoalchemy2.Geometry("MULTIPOLYGON", srid=4326, spatial_index=False)),
    )
    op.execute(
        "CREATE INDEX ix_gazetteer_name_search_trgm ON gazetteer USING gin (name_search gin_trgm_ops)"
    )
    op.execute("CREATE INDEX ix_gazetteer_geom ON gazetteer USING gist (geom)")
    op.execute(
        "CREATE UNIQUE INDEX uq_gazetteer_katottg ON gazetteer (katottg) WHERE katottg IS NOT NULL"
    )
    op.create_index("ix_gazetteer_name_oblast", "gazetteer", ["name_uk", "oblast"])

    op.create_table(
        "news_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("external_id", sa.Text()),
        sa.Column("content_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("title", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("url", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("llm_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("llm_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("visible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint(_in("llm_status", LLM_STATUSES), name="llm_status"),
    )
    op.execute(
        "CREATE INDEX ix_news_items_pending ON news_items (llm_status) WHERE llm_status = 'pending'"
    )
    op.execute("CREATE INDEX ix_news_items_published_at ON news_items (published_at DESC)")
    op.create_index("ix_news_items_source_external", "news_items", ["source_id", "external_id"])

    op.create_table(
        "extracted_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "news_item_id",
            sa.BigInteger(),
            sa.ForeignKey("news_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("geom", geoalchemy2.Geometry("POINT", srid=4326, spatial_index=False)),
        sa.Column("place_name_raw", sa.Text()),
        sa.Column("gazetteer_id", sa.Integer(), sa.ForeignKey("gazetteer.id")),
        sa.Column("coord_source", sa.Text()),
        sa.Column("claimed_by", sa.Text()),
        sa.Column("confidence", sa.Float()),
        sa.Column("llm_raw", sa.dialects.postgresql.JSONB()),
        sa.Column("visible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("linked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(_in("event_type", EVENT_TYPES), name="event_type"),
        sa.CheckConstraint(
            "coord_source IS NULL OR coord_source IN ('explicit_coords','gazetteer_match')",
            name="coord_source",
        ),
        sa.CheckConstraint(
            "claimed_by IS NULL OR claimed_by IN ('ru','ua')", name="claimed_by"
        ),
    )
    op.execute("CREATE INDEX ix_extracted_events_geom ON extracted_events USING gist (geom)")
    op.execute(
        "CREATE INDEX ix_extracted_events_type_created ON extracted_events (event_type, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_extracted_events_unplaced ON extracted_events (id) "
        "WHERE geom IS NULL AND place_name_raw IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_extracted_events_unlinked ON extracted_events (id) WHERE NOT linked"
    )

    op.create_table(
        "frontline_claims",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("geom", geoalchemy2.Geometry("POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("gazetteer_id", sa.Integer(), sa.ForeignKey("gazetteer.id")),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.Text()),
        sa.CheckConstraint(_in("direction", CLAIM_DIRECTIONS), name="direction"),
        sa.CheckConstraint(_in("status", CLAIM_STATUSES), name="status"),
    )
    op.execute("CREATE INDEX ix_frontline_claims_geom ON frontline_claims USING gist (geom)")
    op.create_index("ix_frontline_claims_status", "frontline_claims", ["status"])

    op.create_table(
        "evidence_links",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "claim_id",
            sa.BigInteger(),
            sa.ForeignKey("frontline_claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("extracted_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(_in("role", EVIDENCE_ROLES), name="role"),
        sa.UniqueConstraint("claim_id", "event_id", name="uq_evidence_links_claim_event"),
    )
    op.create_index("ix_evidence_links_event", "evidence_links", ["event_id"])

    op.create_table(
        "frontline_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("layer", sa.Text(), nullable=False),
        sa.Column("geom", geoalchemy2.Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False),
        sa.Column("simplified", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("generation_meta", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(_in("layer", SNAPSHOT_LAYERS), name="layer"),
    )
    op.execute(
        "CREATE INDEX ix_frontline_snapshots_layer_built ON frontline_snapshots (layer, built_at DESC)"
    )

    op.create_table(
        "upstream_geometries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("upstream_version", sa.Text()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("geom", geoalchemy2.Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False),
        sa.Column("meta", sa.dialects.postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.execute(
        "CREATE INDEX ix_upstream_geometries_provider_fetched "
        "ON upstream_geometries (provider, fetched_at DESC)"
    )

    op.create_table(
        "blackout_zones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("geom", geoalchemy2.Geometry("POLYGON", srid=4326, spatial_index=False), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_by", sa.Text()),
    )
    op.execute("CREATE INDEX ix_blackout_zones_geom ON blackout_zones USING gist (geom)")

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text()),
        sa.Column("level", sa.Text(), nullable=False, server_default=sa.text("'info'")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(_in("level", NOTIFICATION_LEVELS), name="level"),
    )

    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text()),
        sa.Column("entity_id", sa.Text()),
        sa.Column("detail", sa.dialects.postgresql.JSONB()),
    )
    op.execute("CREATE INDEX ix_audit_log_at ON audit_log (at DESC)")


def downgrade() -> None:
    for table in (
        "audit_log", "admin_users", "notifications", "blackout_zones", "upstream_geometries",
        "frontline_snapshots", "evidence_links", "frontline_claims", "extracted_events",
        "news_items", "gazetteer", "sources",
    ):
        op.drop_table(table)
