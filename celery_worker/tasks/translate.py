"""Translate news items into English for public display.

Runs on the same `llm` queue as extraction and reuses its machinery: rows are claimed with
``FOR UPDATE SKIP LOCKED`` before any proxy call, batches are sized to the model's real context
window, and reasoning is off. The original text is never overwritten — the site shows the English
rendering with a toggle back to the source language.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select, text

from app.config import settings
from app.extensions import db, log
from app.models import NewsItem, Source
from app.services import llm
from app.services.cache import bump_stat, invalidate
from celery_worker.celery_app import celery
from celery_worker.context import with_app_context

MAX_ATTEMPTS = 3
# Bodies are truncated harder than for extraction: the feed shows a headline and a snippet, and a
# full Telegram essay would crowd out the rest of the batch for no display benefit.
MAX_BODY_CHARS = 1200


def _claim(item_ids: list[int] | None) -> tuple[list[NewsItem], int]:
    """Take ownership of the next items to translate. Mirrors the extraction claim."""
    id_query = select(NewsItem.id).with_for_update(skip_locked=True)
    if item_ids:
        id_query = id_query.where(NewsItem.id.in_(item_ids))
    else:
        id_query = (
            id_query.where(NewsItem.translation_status.in_(("pending", "failed")))
            .order_by(NewsItem.id.desc())
            .limit(settings.llm_batch_size * 3)
        )
    claimed = db.session.execute(id_query).scalars().all()
    if not claimed:
        return [], 0

    rows = (
        db.session.execute(
            select(NewsItem, Source.meta)
            .join(Source, Source.id == NewsItem.source_id)
            .where(NewsItem.id.in_(claimed))
            .order_by(NewsItem.id.desc())
        )
        .all()
    )

    skipped = 0
    candidates: list[NewsItem] = []
    for item, source_meta in rows:
        language = (source_meta or {}).get("language")
        if llm.needs_translation(item.title, item.body, language):
            candidates.append(item)
        else:
            # Already English (or nothing to translate): the originals serve as the display text.
            item.translation_status = "skipped"
            skipped += 1

    batch = candidates[: settings.llm_batch_size]
    claimed_at = dt.datetime.now(dt.timezone.utc)
    for item in batch:
        item.translation_status = "processing"
        item.translation_claimed_at = claimed_at
    db.session.commit()
    return batch, skipped


@celery.task(name="tasks.translate_batch")
@with_app_context
def translate_batch(item_ids: list[int] | None = None) -> dict:
    batch, skipped = _claim(item_ids)
    if not batch:
        return {"batch": 0, "skipped": skipped}

    payload = [
        {
            "idx": i,
            "title": (item.title or "")[:400],
            "body": (item.body or "")[:MAX_BODY_CHARS],
        }
        for i, item in enumerate(batch)
    ]
    payload = llm.fit_batch(payload, system_prompt=llm.TRANSLATION_PROMPT)
    if len(payload) < len(batch):
        for item in batch[len(payload):]:
            item.translation_status = "pending"      # release what did not fit
            item.translation_claimed_at = None
        db.session.commit()
        batch = batch[: len(payload)]
    if not batch:
        return {"batch": 0, "skipped": skipped}

    try:
        results = llm.translate_batch(payload)
    except Exception as exc:
        for item in batch:
            item.translation_status = "pending"
            item.translation_claimed_at = None
        db.session.commit()
        log.warning("translation batch failed: %s: %s", type(exc).__name__, exc)
        return {"batch": len(batch), "error": str(exc)[:200], "skipped": skipped}

    translated = failed = 0
    for idx, item in enumerate(batch):
        result = results.get(idx)
        title_en = (result or {}).get("title")
        body_en = (result or {}).get("body")
        if not isinstance(title_en, str) and not isinstance(body_en, str):
            item.translation_status = "failed"
            item.translation_claimed_at = None
            failed += 1
            continue
        item.title_en = title_en.strip() if isinstance(title_en, str) else None
        item.body_en = body_en.strip() if isinstance(body_en, str) else None
        item.translation_status = "done"
        item.translation_claimed_at = None
        translated += 1
    db.session.commit()

    bump_stat("translated", translated)
    if translated:
        invalidate("api:news")
        invalidate("api:events")
    log.info("translated batch=%d ok=%d failed=%d skipped=%d",
             len(batch), translated, failed, skipped)
    return {"batch": len(batch), "translated": translated, "failed": failed, "skipped": skipped}
