"""Source polling: dispatch fan-out, per-source fetch, ingest and failure accounting (§10)."""
from __future__ import annotations

import datetime as dt
import random

from sqlalchemy import select, text

from app.config import settings
from app.extensions import db, log
from app.models import NewsItem, Source, content_hash
from app.extensions import redis_client
from app.services.cache import bump_stat, invalidate
from celery_worker.adapters import rss, telegram_web
from celery_worker.adapters.http import FetchError, Forbidden
from celery_worker.celery_app import celery
from celery_worker.context import with_app_context

def _queue_depth(queue: str) -> int:
    try:
        return int(redis_client.llen(queue) or 0)
    except Exception:
        return 0


DEGRADED_AT = 5
DEAD_AT = 20
EMPTY_PAGE_LIMIT = 5


@celery.task(name="tasks.dispatch_polls")
@with_app_context
def dispatch_polls() -> dict:
    """Fan out poll_source for every due, enabled source with a random stagger (PLAN §10).

    The fan-out is bounded by how much work is already queued. Without that check this task
    enqueued up to 200 polls a minute regardless of whether the previous round had drained; with
    210 enabled sources and a couple of slow feeds it built a 1,100-task backlog that starved
    every other task on the queue — including the dispatcher that feeds extraction and
    translation.
    """
    now = dt.datetime.now(dt.timezone.utc)
    depth = _queue_depth("default")
    if depth >= settings.poll_queue_max:
        log.info("dispatch_polls skipped: %d tasks already queued", depth)
        return {"dispatched": 0, "queue_depth": depth, "reason": "queue busy"}
    rows = db.session.execute(
        text(
            """
            SELECT id, poll_interval_s FROM sources
            WHERE enabled AND status <> 'dead' AND type IN ('rss','telegram')
              AND (last_polled_at IS NULL
                   OR last_polled_at < now() - make_interval(secs => poll_interval_s))
            ORDER BY last_polled_at NULLS FIRST
            LIMIT :limit
            """
        ),
        {"limit": max(0, settings.poll_queue_max - depth)},
    ).all()
    dispatched = 0
    for row in rows:
        jitter = random.uniform(0, settings.poll_jitter_s)
        poll_source.apply_async(args=[row.id], countdown=jitter)
        dispatched += 1
    log.info("dispatch_polls queued=%d at=%s", dispatched, now.isoformat())
    return {"dispatched": dispatched}


@celery.task(name="tasks.poll_source", bind=True, max_retries=0)
@with_app_context
def poll_source(self, source_id: int) -> dict:
    source = db.session.get(Source, source_id)
    if source is None or not source.is_pollable:
        return {"source_id": source_id, "skipped": True}

    source.last_polled_at = dt.datetime.now(dt.timezone.utc)
    try:
        if source.type == "rss":
            items, state = rss.poll(source)
        elif source.type == "telegram":
            items, state = telegram_web.poll(source)
        else:
            db.session.commit()
            return {"source_id": source_id, "skipped": f"type {source.type} not polled here"}
    except Forbidden as exc:
        # 403: datacenter-IP block. Degraded, never dead, retried at most once per interval.
        source.status = "degraded"
        source.consecutive_failures += 1
        db.session.commit()
        bump_stat("polls_failed")
        log.warning("poll 403 source=%s %s", source_id, exc)
        return {"source_id": source_id, "error": "forbidden"}
    except Exception as exc:
        source.consecutive_failures += 1
        source.status = _status_for(source.consecutive_failures)
        db.session.commit()
        bump_stat("polls_failed")
        log.warning("poll failed source=%s %s: %s", source_id, type(exc).__name__, exc)
        return {"source_id": source_id, "error": str(exc)[:200]}

    if state.get("not_modified"):
        source.consecutive_failures = 0
        source.status = "ok"
        source.last_success_at = dt.datetime.now(dt.timezone.utc)
        db.session.commit()
        bump_stat("polls_ok")
        return {"source_id": source_id, "not_modified": True}

    # Telegram markup drift detector: repeated 200s that parse to nothing → degraded (PLAN §21).
    if state.get("empty_page"):
        meta = dict(source.meta or {})
        empties = int(meta.get("empty_pages", 0)) + 1
        meta["empty_pages"] = empties
        source.meta = meta
        if empties >= EMPTY_PAGE_LIMIT:
            source.status = "degraded"
            log.warning("source %s parsed 0 messages %d times — markup drift?", source_id, empties)
    else:
        meta = dict(source.meta or {})
        meta.pop("empty_pages", None)
        if state.get("meta_update"):
            meta.update(state["meta_update"])
        source.meta = meta

    if "etag" in state:
        source.etag = state.get("etag")
        source.last_modified = state.get("last_modified")

    inserted = ingest_items(source, items)
    source.consecutive_failures = 0
    if source.status != "degraded" or not state.get("empty_page"):
        source.status = "ok" if not state.get("empty_page") else source.status
    source.last_success_at = dt.datetime.now(dt.timezone.utc)
    db.session.commit()

    bump_stat("polls_ok")
    bump_stat("items_ingested", inserted)
    if inserted:
        invalidate("api:news")
    log.info(
        "polled source=%s parsed=%d new=%d inserted=%d",
        source_id, state.get("parsed_total", len(items)), len(items), inserted,
    )
    return {
        "source_id": source_id,
        "parsed": state.get("parsed_total", len(items)),
        "new": len(items),
        "inserted": inserted,
    }


def _status_for(failures: int) -> str:
    if failures >= DEAD_AT:
        return "dead"
    if failures >= DEGRADED_AT:
        return "degraded"
    return "ok"


def ingest_items(source: Source, items: list[dict]) -> int:
    """Insert new items, skipping duplicates by content hash (PLAN §7 UNIQUE constraint)."""
    inserted = 0
    for item in items:
        title = (item.get("title") or "").strip() or None
        body = (item.get("body") or "").strip() or None
        if not title and not body:
            continue
        digest = content_hash(title, body)
        exists = db.session.execute(
            select(NewsItem.id).where(NewsItem.content_hash == digest)
        ).scalar()
        if exists:
            continue
        news = NewsItem(
            source_id=source.id,
            external_id=item.get("external_id"),
            content_hash=digest,
            title=title,
            body=body,
            url=item.get("url"),
            published_at=item.get("published_at"),
            llm_status="pending",
        )
        # Savepoint per item: a duplicate racing in from another worker must not discard the
        # items already inserted in this batch.
        try:
            with db.session.begin_nested():
                db.session.add(news)
                db.session.flush()
            inserted += 1
        except Exception as exc:  # concurrent insert of the same content hash
            log.debug("dedupe collision for %s: %s", digest[:12], exc)
    return inserted
