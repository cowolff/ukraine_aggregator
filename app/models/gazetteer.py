from __future__ import annotations

from geoalchemy2 import Geometry
from sqlalchemy import Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class GazetteerEntry(db.Model):
    """Local place database. The only geocoder in the system (PLAN §12)."""

    __tablename__ = "gazetteer"
    __table_args__ = (
        Index(
            "ix_gazetteer_name_search_trgm",
            "name_search",
            postgresql_using="gin",
            postgresql_ops={"name_search": "gin_trgm_ops"},
        ),
        Index("ix_gazetteer_geom", "geom", postgresql_using="gist"),
        Index("uq_gazetteer_katottg", "katottg", unique=True, postgresql_where=text("katottg IS NOT NULL")),
        Index("ix_gazetteer_name_oblast", "name_uk", "oblast"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    katottg: Mapped[str | None] = mapped_column(Text)
    name_uk: Mapped[str] = mapped_column(Text, nullable=False)
    name_ru: Mapped[str | None] = mapped_column(Text)
    name_en: Mapped[str | None] = mapped_column(Text)
    name_search: Mapped[str] = mapped_column(Text, nullable=False)
    oblast: Mapped[str | None] = mapped_column(Text)
    raion: Mapped[str | None] = mapped_column(Text)
    population: Mapped[int | None] = mapped_column(Integer)
    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    boundary = mapped_column(Geometry("MULTIPOLYGON", srid=4326, spatial_index=False))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Gazetteer {self.id} {self.name_uk} ({self.oblast})>"
