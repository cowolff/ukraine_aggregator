from __future__ import annotations

import datetime as dt

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.sources import JSONType

CLAIM_DIRECTIONS = ("ru_advance", "ua_advance")
CLAIM_STATUSES = ("pending", "confirmed", "rejected", "reverted")
EVIDENCE_ROLES = ("support", "geolocation_proof", "debunk")
SNAPSHOT_LAYERS = ("ru", "grey")


class FrontlineClaim(db.Model):
    __tablename__ = "frontline_claims"
    __table_args__ = (
        CheckConstraint(f"direction IN {CLAIM_DIRECTIONS!r}", name="direction"),
        CheckConstraint(f"status IN {CLAIM_STATUSES!r}", name="status"),
        Index("ix_frontline_claims_geom", "geom", postgresql_using="gist"),
        Index("ix_frontline_claims_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    gazetteer_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("gazetteer.id"))
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)

    gazetteer_entry = relationship("GazetteerEntry", lazy="joined")
    evidence = relationship(
        "EvidenceLink", back_populates="claim", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Claim {self.id} {self.direction} {self.status}>"


class EvidenceLink(db.Model):
    __tablename__ = "evidence_links"
    __table_args__ = (
        CheckConstraint(f"role IN {EVIDENCE_ROLES!r}", name="role"),
        UniqueConstraint("claim_id", "event_id", name="uq_evidence_links_claim_event"),
        Index("ix_evidence_links_event", "event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    claim_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("frontline_claims.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("extracted_events.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    claim = relationship("FrontlineClaim", back_populates="evidence")
    event = relationship("ExtractedEvent", lazy="joined")


class FrontlineSnapshot(db.Model):
    __tablename__ = "frontline_snapshots"
    __table_args__ = (
        CheckConstraint(f"layer IN {SNAPSHOT_LAYERS!r}", name="layer"),
        Index("ix_frontline_snapshots_layer_built", "layer", text("built_at DESC")),
        Index("ix_frontline_snapshots_layer_valid", "layer", text("valid_at DESC")),
        Index("uq_frontline_snapshots_layer_valid", "layer", "valid_at", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    built_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # The instant this geometry depicts. For a live build it equals built_at; for a backfilled
    # historical snapshot it is the upstream snapshot's own timestamp.
    valid_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    layer: Mapped[str] = mapped_column(Text, nullable=False)
    geom = mapped_column(Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False)
    simplified: Mapped[dict] = mapped_column(JSONType, nullable=False)
    generation_meta: Mapped[dict] = mapped_column(JSONType, nullable=False)


class UpstreamGeometry(db.Model):
    """Raw upstream control layers (DeepStateMap, ISW) kept verbatim as builder input.

    Storing them separately means a builder rerun never has to re-fetch, so reverts and
    nightly rebuilds are cheap and deterministic (PLAN §14.3 "recompute from scratch").
    """

    __tablename__ = "upstream_geometries"
    __table_args__ = (
        Index("ix_upstream_geometries_provider_fetched", "provider", text("fetched_at DESC")),
        Index("ix_upstream_geometries_provider_valid", "provider", text("valid_at DESC")),
        Index(
            "uq_upstream_geometries_provider_version",
            "provider",
            "upstream_version",
            unique=True,
            postgresql_where=text("upstream_version IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # 'deepstate' | 'isw'
    upstream_version: Mapped[str | None] = mapped_column(Text)  # deepstate id / isw EditDate
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    valid_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    geom = mapped_column(Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False)
    meta: Mapped[dict] = mapped_column(JSONType, nullable=False, server_default=text("'{}'"))
