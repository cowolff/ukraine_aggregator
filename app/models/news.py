from __future__ import annotations

import datetime as dt
import hashlib
import re

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

# 'processing' is a claim marker: a worker owns the row while its batch is in flight, so
# concurrent extraction workers never pick the same item twice.
LLM_STATUSES = ("pending", "processing", "done", "skipped", "failed")

_WS = re.compile(r"\s+")


def content_hash(title: str | None, body: str | None) -> str:
    """sha256 over normalised title+body — the ingest dedupe key (PLAN §7)."""
    norm = _WS.sub(" ", f"{(title or '').strip()}\n{(body or '').strip()}").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


class NewsItem(db.Model):
    __tablename__ = "news_items"
    __table_args__ = (
        CheckConstraint(f"llm_status IN {LLM_STATUSES!r}", name="llm_status"),
        CheckConstraint(f"translation_status IN {LLM_STATUSES!r}", name="translation_status"),
        Index("ix_news_items_pending", "llm_status", postgresql_where=text("llm_status = 'pending'")),
        Index("ix_news_items_published_at", text("published_at DESC")),
        Index("ix_news_items_source_external", "source_id", "external_id"),
        Index(
            "ix_news_items_processing",
            "llm_claimed_at",
            postgresql_where=text("llm_status = 'processing'"),
        ),
        Index(
            "ix_news_items_translation_pending",
            text("id DESC"),
            postgresql_where=text("translation_status IN ('pending', 'failed')"),
        ),
        Index(
            "ix_news_items_translation_processing",
            "translation_claimed_at",
            postgresql_where=text("translation_status = 'processing'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    llm_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    llm_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # When a worker claimed this row. Used only to recover claims abandoned by a dead worker.
    llm_claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    visible: Mapped[bool] = mapped_column(db.Boolean, nullable=False, server_default=text("true"))
    # English rendering for public display. The original is never overwritten.
    title_en: Mapped[str | None] = mapped_column(Text)
    body_en: Mapped[str | None] = mapped_column(Text)
    translation_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    translation_claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    source = relationship("Source", lazy="joined")
    events = relationship(
        "ExtractedEvent", back_populates="news_item", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NewsItem {self.id} {(self.title or '')[:40]!r}>"
