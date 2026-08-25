"""English summaries: one general summary per item, one focused summary per map location.

Runs on the `llm` queue with the same machinery as translation: `FOR UPDATE SKIP LOCKED`
claims, context-window batch sizing, reasoning off. It is gated on extraction having settled
(llm_status no longer pending/processing) so the item's extracted place names exist — the prompt
asks for one location summary per *distinct* place name and the result is fanned out to every
event sharing that name, so an item with many markers at one settlement costs a single entry.
Items whose body carries nothing beyond the headline are skipped: there is no content to
summarise, and the (translated) title already serves as the display text.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import select

from app.config import settings
from app.extensions import db, log
from app.models import ExtractedEvent, NewsItem
from app.services import llm
from app.services.cache import bump_stat, invalidate
from celery_worker.celery_app import celery
from celery_worker.context import with_app_context

# More generous than translation's cap: a faithful summary needs the item's substance
# (fit_batch trims further when the batch would not fit the context window).
MAX_BODY_CHARS = 3000

# Half the shared batch size: summaries are the *output*, and a full batch of multi-location
# items times 3-5 sentences each would press against LLM_MAX_TOKENS — a completion cut off at
# the ceiling is unparseable JSON, failing the whole batch. Fewer items per call, same budget.
def _batch_size() -> int:
    return max(1, settings.llm_batch_size // 2)

_WS = re.compile(r"\s+")


def _has_content(item: NewsItem) -> bool:
    """A summary needs text beyond the headline; feeds often copy the title into the body."""
    body = _WS.sub(" ", (item.body or "")).strip().lower()
    title = _WS.sub(" ", (item.title or "")).strip().lower()
    return bool(body) and body != title


def _claim(item_ids: list[int] | None) -> tuple[list[NewsItem], int]:
    """Take ownership of the next items to summarise. Mirrors the translation claim."""
    id_query = select(NewsItem.id).with_for_update(skip_locked=True)
    if item_ids:
        id_query = id_query.where(NewsItem.id.in_(item_ids))
    else:
        id_query = (
            id_query.where(
                NewsItem.summary_status.in_(("pending", "failed")),
                # Wait for extraction to settle so the location list is the map's location list.
                NewsItem.llm_status.not_in(("pending", "processing")),
            )
            .order_by(NewsItem.id.desc())
            .limit(_batch_size() * 3)
        )
    claimed = db.session.execute(id_query).scalars().all()
    if not claimed:
        return [], 0

    rows = (
        db.session.execute(
            select(NewsItem).where(NewsItem.id.in_(claimed)).order_by(NewsItem.id.desc())
        )
        .scalars()
        .all()
    )

    skipped = 0
    candidates: list[NewsItem] = []
    for item in rows:
        # Judged off-topic → never shown in the feed, so a summary would never be read.
        if item.judged_irrelevant or not _has_content(item):
            item.summary_status = "skipped"
            skipped += 1
        else:
            candidates.append(item)

    batch = candidates[: _batch_size()]
    claimed_at = dt.datetime.now(dt.timezone.utc)
    for item in batch:
        item.summary_status = "processing"
        item.summary_claimed_at = claimed_at
    db.session.commit()
    return batch, skipped


def _location_names(batch: list[NewsItem]) -> dict[int, list[str]]:
    """Distinct extracted place names per item, in event order — the map's own location set."""
    rows = db.session.execute(
        select(ExtractedEvent.news_item_id, ExtractedEvent.place_name_raw)
        .where(
            ExtractedEvent.news_item_id.in_([item.id for item in batch]),
            ExtractedEvent.visible,
            ExtractedEvent.place_name_raw.is_not(None),
        )
        .order_by(ExtractedEvent.id)
    ).all()
    names: dict[int, list[str]] = {}
    seen: set[tuple[int, str]] = set()
    for item_id, name in rows:
        key = (item_id, name.strip().lower())
        if not name.strip() or key in seen:
            continue
        seen.add(key)
        names.setdefault(item_id, []).append(name.strip())
    return names


@celery.task(name="tasks.summarize_batch")
@with_app_context
def summarize_batch(item_ids: list[int] | None = None) -> dict:
    batch, skipped = _claim(item_ids)
    if not batch:
        return {"batch": 0, "skipped": skipped}

    locations = _location_names(batch)
    payload = [
        {
            "idx": i,
            "title": (item.title or "")[:400],
            "body": (item.body or "")[:MAX_BODY_CHARS],
            "locations": locations.get(item.id, []),
        }
        for i, item in enumerate(batch)
    ]
    payload = llm.fit_batch(payload, system_prompt=llm.SUMMARY_PROMPT)
    if len(payload) < len(batch):
        for item in batch[len(payload):]:
            item.summary_status = "pending"      # release what did not fit
            item.summary_claimed_at = None
        db.session.commit()
        batch = batch[: len(payload)]
    if not batch:
        return {"batch": 0, "skipped": skipped}

    try:
        results = llm.summarize_batch(payload)
    except Exception as exc:
        for item in batch:
            item.summary_status = "pending"
            item.summary_claimed_at = None
        db.session.commit()
        log.warning("summary batch failed: %s: %s", type(exc).__name__, exc)
        return {"batch": len(batch), "error": str(exc)[:200], "skipped": skipped}

    summarized = failed = placed = 0
    for idx, item in enumerate(batch):
        result = results.get(idx)
        summary = (result or {}).get("summary")
        if not isinstance(summary, str) or not summary.strip():
            item.summary_status = "failed"
            item.summary_claimed_at = None
            failed += 1
            continue
        item.summary_en = summary.strip()
        placed += _fan_out_locations(item, (result or {}).get("locations"))
        item.summary_status = "done"
        item.summary_claimed_at = None
        summarized += 1
    db.session.commit()

    bump_stat("summarized", summarized)
    if summarized:
        invalidate("api:news")
        invalidate("api:events")
    log.info("summarized batch=%d ok=%d failed=%d skipped=%d location_summaries=%d",
             len(batch), summarized, failed, skipped, placed)
    return {"batch": len(batch), "summarized": summarized, "failed": failed,
            "skipped": skipped, "location_summaries": placed}


def _fan_out_locations(item: NewsItem, entries) -> int:
    """Write each location summary onto every event of the item that carries that place name."""
    if not isinstance(entries, list):
        return 0
    by_name: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name, summary = entry.get("name"), entry.get("summary")
        if isinstance(name, str) and isinstance(summary, str) and summary.strip():
            by_name[name.strip().lower()] = summary.strip()
    if not by_name:
        return 0
    written = 0
    for event in item.events:
        summary = by_name.get((event.place_name_raw or "").strip().lower())
        if summary:
            event.summary_en = summary
            written += 1
    return written
