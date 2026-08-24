from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

PERSPECTIVES = ("ukrainian", "russian", "western", "neutral")
SOURCE_TYPES = ("rss", "telegram", "api", "scrape")
SOURCE_STATUSES = ("ok", "degraded", "dead")

JSONType = JSONB().with_variant(JSON(), "sqlite")


class Source(db.Model):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(f"type IN {SOURCE_TYPES!r}", name="type"),
        CheckConstraint(f"perspective IN {PERSPECTIVES!r}", name="perspective"),
        CheckConstraint(f"status IN {SOURCE_STATUSES!r}", name="status"),
        Index("ix_sources_due", "enabled", "last_polled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    perspective: Mapped[str] = mapped_column(Text, nullable=False)
    reliability_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("2"))
    poll_interval_s: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("900"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_polled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ok'"))
    meta: Mapped[dict] = mapped_column(JSONType, nullable=False, server_default=text("'{}'"))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Source {self.id} {self.type}:{self.name}>"

    @property
    def is_pollable(self) -> bool:
        return self.enabled and self.status != "dead"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "url": self.url,
            "perspective": self.perspective,
            "reliability_tier": self.reliability_tier,
            "poll_interval_s": self.poll_interval_s,
            "enabled": self.enabled,
            "status": self.status,
            "last_polled_at": self.last_polled_at.isoformat() if self.last_polled_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "consecutive_failures": self.consecutive_failures,
            "meta": self.meta or {},
        }
