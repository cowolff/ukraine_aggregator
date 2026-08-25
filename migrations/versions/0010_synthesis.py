"""Cross-source synthesized reports (plans/SYNTHESIS.md).

When several distinct sources report same-type events within SYNTH_JOIN_KM of each other inside
a rolling window, the cluster becomes a synthesized_reports row: an LLM-written merged summary
for the general news rail, plus a code-derived credibility verdict weighted by the member
sources' reliability tiers and perspective classes. synthesis_members links the cluster's
extracted events, mirroring frontline_claims/evidence_links.

Revision ID: 0010_synthesis
Revises: 0009_summaries
"""
from __future__ import annotations

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision = "0010_synthesis"
down_revision = "0009_summaries"
branch_labels = None
depends_on = None

STATUSES = ("open", "closed")
LLM_STATUSES = ("pending", "processing", "done", "failed")
VERDICTS = ("confirmed", "corroborated", "reported", "unverified")


def upgrade() -> None:
    op.create_table(
        "synthesized_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.Geometry("POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("gazetteer_id", sa.Integer(), sa.ForeignKey("gazetteer.id")),
        sa.Column("place_name", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("llm_status", sa.Text()),
        sa.Column("llm_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("llm_claimed_at", sa.DateTime(timezone=True)),
        sa.Column("headline_en", sa.Text()),
        sa.Column("summary_en", sa.Text()),
        sa.Column("disagreements_en", sa.Text()),
        sa.Column("credibility", sa.Text()),
        sa.Column("cred_meta", sa.dialects.postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "synthesized_member_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("first_reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("visible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_check_constraint("status", "synthesized_reports", f"status IN {STATUSES!r}")
    op.create_check_constraint(
        "llm_status", "synthesized_reports",
        f"llm_status IS NULL OR llm_status IN {LLM_STATUSES!r}",
    )
    op.create_check_constraint(
        "credibility", "synthesized_reports",
        f"credibility IS NULL OR credibility IN {VERDICTS!r}",
    )
    op.execute(
        "CREATE INDEX ix_synthesized_reports_geom ON synthesized_reports USING gist (geom)"
    )
    op.execute(
        "CREATE INDEX ix_synthesized_reports_open ON synthesized_reports (event_type) "
        "WHERE status = 'open'"
    )
    op.execute(
        "CREATE INDEX ix_synthesized_reports_llm_pending ON synthesized_reports (id) "
        "WHERE llm_status IN ('pending', 'failed')"
    )
    op.execute(
        "CREATE INDEX ix_synthesized_reports_llm_processing ON synthesized_reports "
        "(llm_claimed_at) WHERE llm_status = 'processing'"
    )
    op.execute(
        "CREATE INDEX ix_synthesized_reports_last_reported ON synthesized_reports "
        "(last_reported_at DESC)"
    )

    op.create_table(
        "synthesis_members",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "report_id",
            sa.BigInteger(),
            sa.ForeignKey("synthesized_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("extracted_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("report_id", "event_id", name="uq_synthesis_members_report_event"),
    )
    op.create_index("ix_synthesis_members_event", "synthesis_members", ["event_id"])


def downgrade() -> None:
    op.drop_table("synthesis_members")
    op.drop_table("synthesized_reports")
