"""Cross-source synthesis tasks (plans/SYNTHESIS.md).

Two stages, decoupled through DB status columns like the rest of the pipeline:

* ``tasks.cluster_synthesis`` (default queue, beat): the cheap SQL/Python pass — join or found
  clusters, close expired ones, compute credibility, arm the LLM stage. All logic lives in
  ``app/services/synthesis.py``; this is a thin wrapper like ``tasks.evaluate_claims``.
* ``tasks.synthesize_reports_batch`` (llm queue, via dispatch_llm): claim armed reports with
  ``FOR UPDATE SKIP LOCKED``, feed the member reports to the LLM, persist headline/summary/
  disagreements. Same claim machinery as summarisation.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.config import settings
from app.extensions import db, log
from app.models import SynthesizedReport
from app.services import llm
from app.services.cache import bump_stat, invalidate
from app.services.rules import PERSPECTIVE_CLASS
from app.services.synthesis import cluster_events
from celery_worker.celery_app import celery
from celery_worker.context import with_app_context

MAX_SYNTH_ATTEMPTS = 3  # like extraction: a poisoned report retries at most twice more

# Member snippets are inputs, not the display text — enough substance to merge faithfully
# without a single verbose member crowding out the others (fit_batch trims further).
MAX_MEMBER_CHARS = 500


# A synthesis item carries every member report inline, so it is far heavier than a summary item.
def _batch_size() -> int:
    return max(1, settings.llm_batch_size // 4)


@celery.task(name="tasks.cluster_synthesis")
@with_app_context
def cluster_synthesis() -> dict:
    return cluster_events().as_dict()


def _claim(report_ids: list[int] | None) -> list[SynthesizedReport]:
    """Take ownership of the next armed reports. Mirrors the extraction claim."""
    id_query = select(SynthesizedReport.id).with_for_update(skip_locked=True)
    if report_ids:
        id_query = id_query.where(SynthesizedReport.id.in_(report_ids))
    else:
        id_query = (
            id_query.where(
                SynthesizedReport.llm_status.in_(("pending", "failed")),
                SynthesizedReport.llm_attempts < MAX_SYNTH_ATTEMPTS,
            )
            .order_by(SynthesizedReport.llm_attempts, SynthesizedReport.id.desc())
            .limit(_batch_size())
        )
    claimed = db.session.execute(id_query).scalars().all()
    if not claimed:
        return []
    batch = (
        db.session.execute(
            select(SynthesizedReport)
            .where(SynthesizedReport.id.in_(claimed))
            .order_by(SynthesizedReport.id.desc())
        )
        .scalars()
        .all()
    )
    claimed_at = dt.datetime.now(dt.timezone.utc)
    for report in batch:
        report.llm_status = "processing"
        report.llm_claimed_at = claimed_at
    db.session.commit()  # releases the row locks before the (slow) proxy call
    return batch


def _member_reports(report_id: int) -> list[dict]:
    """One prompt entry per distinct member news item, resolved to source metadata."""
    from sqlalchemy import text

    rows = db.session.execute(
        text(
            """
            SELECT DISTINCT ON (n.id)
                   n.id AS news_item_id,
                   s.name AS source_name, s.perspective, s.reliability_tier,
                   COALESCE(n.published_at, n.fetched_at) AS published_at,
                   COALESCE(n.title_en, n.title) AS title,
                   COALESCE(n.summary_en, n.body_en, n.body) AS content,
                   bool_or(e.event_type = 'debunk') OVER (PARTITION BY n.id) AS disputes
            FROM synthesis_members m
            JOIN extracted_events e ON e.id = m.event_id
            JOIN news_items n ON n.id = e.news_item_id
            JOIN sources s ON s.id = n.source_id
            WHERE m.report_id = :rid
            ORDER BY n.id
            """
        ),
        {"rid": report_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def _sample_members(rows: list[dict]) -> tuple[list[dict], int]:
    """Cap the members fed to the prompt: round-robin across perspective classes, best tier
    (then newest) first within each class — maximum diversity under the cap."""
    cap = settings.synth_max_members_in_prompt
    by_class: dict[str, list[dict]] = {}
    for row in rows:
        by_class.setdefault(PERSPECTIVE_CLASS.get(row["perspective"], "western"), []).append(row)
    for members in by_class.values():
        members.sort(
            key=lambda r: (
                int(r["reliability_tier"]),
                -(r["published_at"].timestamp() if r["published_at"] else 0),
            )
        )
    picked: list[dict] = []
    while len(picked) < cap and any(by_class.values()):
        for cls in sorted(by_class):
            if by_class[cls] and len(picked) < cap:
                picked.append(by_class[cls].pop(0))
    return picked, len(rows) - len(picked)


def _payload_entry(idx: int, report: SynthesizedReport, rows: list[dict]) -> dict:
    picked, leftover = _sample_members(rows)
    entries = []
    for row in picked:
        entry = {
            "source": row["source_name"],
            "perspective": row["perspective"],
            "tier": int(row["reliability_tier"]),
            "at": row["published_at"].isoformat() if row["published_at"] else None,
            "title": (row["title"] or "")[:300],
            "summary": (row["content"] or "")[:MAX_MEMBER_CHARS],
        }
        if row["disputes"]:
            entry["disputes"] = True
        entries.append(entry)
    return {
        "idx": idx,
        "event_type": report.event_type,
        "place": report.place_name,
        "verdict": report.credibility,
        "reports": entries,
        "additional_reports": leftover,
    }


@celery.task(name="tasks.synthesize_reports_batch")
@with_app_context
def synthesize_reports_batch(report_ids: list[int] | None = None) -> dict:
    batch = _claim(report_ids)
    if not batch:
        return {"batch": 0}

    payload = [_payload_entry(i, report, _member_reports(report.id)) for i, report in enumerate(batch)]
    payload = llm.fit_batch(payload, system_prompt=llm.SYNTHESIS_PROMPT)
    if len(payload) < len(batch):
        for report in batch[len(payload):]:
            report.llm_status = "pending"  # release what did not fit
            report.llm_claimed_at = None
        db.session.commit()
        batch = batch[: len(payload)]
    if not batch:
        return {"batch": 0}

    try:
        results = llm.synthesize_batch(payload)
    except Exception as exc:
        # Same policy as extraction: attempts retire poison reports, outages spend nothing.
        transient = llm.is_transient(exc)
        for report in batch:
            if transient:
                report.llm_status = "pending"
            else:
                report.llm_attempts += 1
                report.llm_status = "failed"
            report.llm_claimed_at = None
        db.session.commit()
        log.warning("synthesis batch failed%s: %s: %s",
                    " (transient, no attempt spent)" if transient else "",
                    type(exc).__name__, exc)
        return {"batch": len(batch), "error": str(exc)[:200]}

    synthesized = failed = 0
    for idx, report in enumerate(batch):
        result = results.get(idx) or {}
        headline = result.get("headline")
        summary = result.get("summary")
        if not (isinstance(headline, str) and headline.strip()
                and isinstance(summary, str) and summary.strip()):
            report.llm_attempts += 1
            report.llm_status = "failed"
            report.llm_claimed_at = None
            failed += 1
            continue
        disagreements = result.get("disagreements")
        report.headline_en = headline.strip()
        report.summary_en = summary.strip()
        report.disagreements_en = (
            disagreements.strip() if isinstance(disagreements, str) and disagreements.strip()
            else None
        )
        report.synthesized_member_count = report.member_count
        report.llm_status = "done"
        report.llm_claimed_at = None
        report.updated_at = dt.datetime.now(dt.timezone.utc)
        synthesized += 1
    db.session.commit()

    bump_stat("synthesized_reports", synthesized)
    if synthesized:
        invalidate("api:synthesis")
    log.info("synthesized reports batch=%d ok=%d failed=%d", len(batch), synthesized, failed)
    return {"batch": len(batch), "synthesized": synthesized, "failed": failed}
