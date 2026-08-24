"""Keep the LLM queue fed.

Beat originally fired a single `llm_extract_batch` and a single `translate_batch` every 120 s.
That is ~8 items per task per two minutes — about 240 items/hour against an ingest rate of roughly
230 items/hour, so the pipeline had no headroom at all: a backlog could never be worked off, and
any dip left the newest (most visible) items untranslated.

This dispatcher instead keeps the `llm` queue topped up to a small target depth, split between
extraction and translation in proportion to what is actually waiting. Workers stay busy, the queue
never grows without bound, and whichever kind of work is behind gets the capacity.
"""
from __future__ import annotations

from sqlalchemy import text

from app.config import settings
from app.extensions import db, log, redis_client
from celery_worker.celery_app import celery
from celery_worker.context import with_app_context

LLM_QUEUE = "llm"


def _queue_depth() -> int:
    try:
        return int(redis_client.llen(LLM_QUEUE) or 0)
    except Exception:
        return 0


@celery.task(name="tasks.dispatch_llm")
@with_app_context
def dispatch_llm() -> dict:
    target = settings.llm_queue_target
    depth = _queue_depth()
    room = target - depth
    if room <= 0:
        return {"queued": 0, "depth": depth, "reason": "queue already at target"}

    counts = db.session.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM news_items
                WHERE llm_status IN ('pending','failed') AND llm_attempts < 3) AS extract_pending,
              (SELECT count(*) FROM news_items
                WHERE translation_status IN ('pending','failed')) AS translate_pending
            """
        )
    ).mappings().first()

    extract_pending = int(counts["extract_pending"])
    translate_pending = int(counts["translate_pending"])
    total = extract_pending + translate_pending
    if total == 0:
        return {"queued": 0, "depth": depth, "reason": "nothing pending"}

    # Split the free slots in proportion to what is waiting, but never starve a kind that has
    # work: whichever is behind gets the bulk, the other still gets at least one slot.
    extract_slots = round(room * extract_pending / total) if extract_pending else 0
    translate_slots = room - extract_slots
    if extract_pending and extract_slots == 0:
        extract_slots, translate_slots = 1, max(0, translate_slots - 1)
    if translate_pending and translate_slots == 0:
        translate_slots, extract_slots = 1, max(0, extract_slots - 1)

    from celery_worker.tasks.extract import llm_extract_batch
    from celery_worker.tasks.translate import translate_batch

    for _ in range(extract_slots):
        llm_extract_batch.apply_async(queue=LLM_QUEUE)
    for _ in range(translate_slots):
        translate_batch.apply_async(queue=LLM_QUEUE)

    result = {
        "queued": extract_slots + translate_slots,
        "extract": extract_slots,
        "translate": translate_slots,
        "depth_before": depth,
        "extract_pending": extract_pending,
        "translate_pending": translate_pending,
    }
    log.info("dispatch_llm %s", result)
    return result
