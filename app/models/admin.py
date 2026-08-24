from __future__ import annotations

import datetime as dt

from flask_login import UserMixin
from geoalchemy2 import Geometry
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.sources import JSONType

NOTIFICATION_LEVELS = ("info", "warning", "alert")


class BlackoutZone(db.Model):
    """Polygon inside which news/events are withheld from the public API (PLAN §17.6).

    Filtering happens at publish time only — ingestion continues, so deactivating a zone
    makes its history reappear.
    """

    __tablename__ = "blackout_zones"
    __table_args__ = (Index("ix_blackout_zones_geom", "geom", postgresql_using="gist"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    geom = mapped_column(Geometry("POLYGON", srid=4326, spatial_index=False), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[str | None] = mapped_column(Text)


class Notification(db.Model):
    __tablename__ = "notifications"
    __table_args__ = (CheckConstraint(f"level IN {NOTIFICATION_LEVELS!r}", name="level"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'info'"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    starts_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AdminUser(UserMixin, db.Model):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)

    def check_password(self, password: str) -> bool:
        import bcrypt

        try:
            return bcrypt.checkpw(password.encode("utf-8"), self.password_hash.encode("utf-8"))
        except (ValueError, TypeError):
            return False

    @staticmethod
    def hash_password(password: str) -> str:
        import bcrypt

        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_at", text("at DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONType)
