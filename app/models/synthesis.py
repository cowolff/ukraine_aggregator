"""Cross-source synthesized reports (plans/SYNTHESIS.md).

A SynthesizedReport is a pipeline-generated meta-report: when several distinct sources report
same-type events within SYNTH_JOIN_KM of each other inside a rolling window, the cluster gets an
LLM-written merged summary plus a *code-derived* credibility verdict. The verdict — like
perspective labels — is never the LLM's to assign; `cred_meta` stores its inputs so it is
auditable. Members link extracted events, mirroring FrontlineClaim/EvidenceLink.
"""
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

SYNTHESIS_STATUSES = ("open", "closed")
# No 'skipped': a report is only armed (llm_status set at all) once it crosses the source
# threshold, so there is nothing to skip. NULL = not armed yet.
SYNTHESIS_LLM_STATUSES = ("pending", "processing", "done", "failed")
CREDIBILITY_VERDICTS = ("confirmed", "corroborated", "reported", "unverified")


class SynthesizedReport(db.Model):
    __tablename__ = "synthesized_reports"
    __table_args__ = (
        CheckConstraint(f"status IN {SYNTHESIS_STATUSES!r}", name="status"),
        CheckConstraint(
            f"llm_status IS NULL OR llm_status IN {SYNTHESIS_LLM_STATUSES!r}", name="llm_status"
        ),
        CheckConstraint(
            f"credibility IS NULL OR credibility IN {CREDIBILITY_VERDICTS!r}", name="credibility"
        ),
        Index("ix_synthesized_reports_geom", "geom", postgresql_using="gist"),
        # The clustering join probe: nearest open report of this event type.
        Index(
            "ix_synthesized_reports_open",
            "event_type",
            postgresql_where=text("status = 'open'"),
        ),
        # The dispatcher's pending count and the synthesize task's claim scan.
        Index(
            "ix_synthesized_reports_llm_pending",
            "id",
            postgresql_where=text("llm_status IN ('pending', 'failed')"),
        ),
        Index(
            "ix_synthesized_reports_llm_processing",
            "llm_claimed_at",
            postgresql_where=text("llm_status = 'processing'"),
        ),
        # The rail window query: newest finished syntheses in a time range.
        Index("ix_synthesized_reports_last_reported", text("last_reported_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    gazetteer_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("gazetteer.id"))
    # Denormalised display name (modal gazetteer entry of the members, else raw place name).
    place_name: Mapped[str | None] = mapped_column(Text)
    # open = still accepts members; closed = window expired, a new burst founds a new report.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    # NULL until the cluster crosses SYNTH_MIN_SOURCES; then the summariser state machine.
    llm_status: Mapped[str | None] = mapped_column(Text)
    llm_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    llm_claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    headline_en: Mapped[str | None] = mapped_column(Text)
    summary_en: Mapped[str | None] = mapped_column(Text)
    # Nullable: only written when the member accounts actually conflict (counts, attribution...).
    disagreements_en: Mapped[str | None] = mapped_column(Text)
    # Code-derived verdict (plans/SYNTHESIS.md §4); cred_meta carries its inputs for audit.
    credibility: Mapped[str | None] = mapped_column(Text)
    cred_meta: Mapped[dict] = mapped_column(JSONType, nullable=False, server_default=text("'{}'"))
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # member_count at the last completed LLM pass — drives the re-synthesis hysteresis.
    synthesized_member_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # min/max member occurred_at — the rail's time axis.
    first_reported_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_reported_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    gazetteer_entry = relationship("GazetteerEntry", lazy="joined")
    members = relationship(
        "SynthesisMember", back_populates="report", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SynthesizedReport {self.id} {self.event_type} {self.status} {self.credibility}>"


class SynthesisMember(db.Model):
    __tablename__ = "synthesis_members"
    __table_args__ = (
        UniqueConstraint("report_id", "event_id", name="uq_synthesis_members_report_event"),
        # The clustering scan's "already a member?" anti-join probe.
        Index("ix_synthesis_members_event", "event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("synthesized_reports.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("extracted_events.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    report = relationship("SynthesizedReport", back_populates="members")
    event = relationship("ExtractedEvent", lazy="joined")
