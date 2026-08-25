from __future__ import annotations

import datetime as dt

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.sources import JSONType

EVENT_TYPES = (
    "frontline_advance",
    "frontline_claim",
    "deep_strike",
    "shelling",
    "geolocation_proof",
    "debunk",
    "other",
)
COORD_SOURCES = ("explicit_coords", "gazetteer_match")

# Event types that can move the line at all; everything else is icon-only (PLAN §13).
FRONTLINE_EVENT_TYPES = ("frontline_advance", "frontline_claim")

# Glyphs used by the SPA legend; kept server-side so /api/meta can hand them to the client.
EVENT_GLYPHS = {
    "frontline_advance": "▲",
    "frontline_claim": "△",
    "deep_strike": "✸",
    "shelling": "●",
    "geolocation_proof": "◎",
    "debunk": "⊘",
    "other": "·",
}


class ExtractedEvent(db.Model):
    __tablename__ = "extracted_events"
    __table_args__ = (
        CheckConstraint(f"event_type IN {EVENT_TYPES!r}", name="event_type"),
        CheckConstraint(
            "coord_source IS NULL OR coord_source IN ('explicit_coords','gazetteer_match')",
            name="coord_source",
        ),
        CheckConstraint("claimed_by IS NULL OR claimed_by IN ('ru','ua')", name="claimed_by"),
        Index("ix_extracted_events_geom", "geom", postgresql_using="gist"),
        Index("ix_extracted_events_type_created", "event_type", text("created_at DESC")),
        Index(
            "ix_extracted_events_unplaced",
            "id",
            postgresql_where=text("geom IS NULL AND place_name_raw IS NOT NULL"),
        ),
        Index("ix_extracted_events_unlinked", "id", postgresql_where=text("NOT linked")),
        # Probe index for the /api/news placement anti-join: "does this item have a placed event".
        Index(
            "ix_extracted_events_item_placed",
            "news_item_id",
            postgresql_where=text("visible AND geom IS NOT NULL"),
        ),
        Index("ix_extracted_events_occurred", text("occurred_at DESC")),
        Index(
            "ix_extracted_events_occurred_geom",
            "geom",
            "occurred_at",
            postgresql_using="gist",
            postgresql_where=text("geom IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news_items.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=False))
    place_name_raw: Mapped[str | None] = mapped_column(Text)
    gazetteer_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("gazetteer.id"))
    coord_source: Mapped[str | None] = mapped_column(Text)
    claimed_by: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    llm_raw: Mapped[dict | None] = mapped_column(JSONType)
    # English summary focused on this event's place — what the item reports as happening *there*.
    # Written by the summary task; events of one item sharing a place name share one summary.
    summary_en: Mapped[str | None] = mapped_column(Text)
    # How the point was placed: {"resolution": "hinted|countrywide|fallback|manual",
    # "similarity": float, "hint": <raw LLM oblast>} — the audit trail for mislocation hunts.
    geo_meta: Mapped[dict | None] = mapped_column(JSONType)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    linked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # When the reported event actually happened, as opposed to when we ingested it. This is the
    # axis the map scrubs along; `created_at` stays as the ingest audit trail.
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    news_item = relationship("NewsItem", back_populates="events")
    gazetteer_entry = relationship("GazetteerEntry", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExtractedEvent {self.id} {self.event_type} {self.place_name_raw!r}>"
