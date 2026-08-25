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
                WHERE translation_status IN ('pending','failed')) AS translate_pending,
              -- Mirrors the summariser's claim gate: an item still waiting on extraction is not
              -- claimable yet, so counting it would waste a slot on an empty batch.
              (SELECT count(*) FROM news_items
                WHERE summary_status IN ('pending','failed')
                  AND llm_status NOT IN ('pending','processing')) AS summarize_pending,
              -- Armed synthesized reports (plans/SYNTHESIS.md). Volume is tiny — clusters, not
              -- items — but it must be in the split so a translation backlog cannot starve it.
              (SELECT count(*) FROM synthesized_reports
                WHERE llm_status IN ('pending','failed') AND llm_attempts < 3) AS synthesize_reports_pending
            """
        )
    ).mappings().first()

    pending = {
        kind: int(counts[f"{kind}_pending"])
        for kind in ("extract", "translate", "summarize", "synthesize_reports")
    }
    total = sum(pending.values())
    if total == 0:
        return {"queued": 0, "depth": depth, "reason": "nothing pending"}

    slots = _split_slots(room, pending)

    from celery_worker.tasks.extract import llm_extract_batch
    from celery_worker.tasks.summarize import summarize_batch
    from celery_worker.tasks.synthesize import synthesize_reports_batch
    from celery_worker.tasks.translate import translate_batch

    tasks = {"extract": llm_extract_batch, "translate": translate_batch,
             "summarize": summarize_batch, "synthesize_reports": synthesize_reports_batch}
    for kind, task in tasks.items():
        for _ in range(slots[kind]):
            task.apply_async(queue=LLM_QUEUE)

    result = {
        "queued": sum(slots.values()),
        **slots,
        "depth_before": depth,
        **{f"{kind}_pending": count for kind, count in pending.items()},
    }
    log.info("dispatch_llm %s", result)
    return result


def _split_slots(room: int, pending: dict[str, int]) -> dict[str, int]:
    """Split the free queue slots in proportion to what is waiting.

    No kind that has work is ever starved: after the proportional split, each waiting kind with
    zero slots takes one from the current largest allocation (when that donor can spare it).
    """
    total = sum(pending.values())
    slots = {kind: room * count // total for kind, count in pending.items()}
    # Hand out the flooring remainder, largest backlog first.
    leftover = room - sum(slots.values())
    for kind in sorted(pending, key=pending.get, reverse=True):
        if leftover <= 0:
            break
        if pending[kind]:
            slots[kind] += 1
            leftover -= 1
    for kind, count in pending.items():
        if count and slots[kind] == 0:
            donor = max(slots, key=slots.get)
            if slots[donor] > 1:
                slots[donor] -= 1
                slots[kind] += 1
    return slots
