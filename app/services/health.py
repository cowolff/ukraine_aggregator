"""/healthz payload and the admin dashboard rollup (PLAN §15, §17.1)."""
from __future__ import annotations

from sqlalchemy import text

from app.extensions import db, redis_client
from app.services.cache import read_stats

STAT_NAMES = [
    "llm_calls", "llm_prompt_tokens", "llm_completion_tokens", "llm_total_tokens",
    "llm_latency_ms_total", "llm_rate_limited", "llm_5xx", "llm_4xx",
    "items_ingested", "events_created", "polls_ok", "polls_failed",
]


def health_report() -> dict:
    report: dict = {"db": "error", "redis": "error"}
    try:
        db.session.execute(text("SELECT 1"))
        report["db"] = "ok"
    except Exception as exc:
        report["db_error"] = str(exc)[:200]
        db.session.rollback()
    try:
        redis_client.ping()
        report["redis"] = "ok"
    except Exception as exc:
        report["redis_error"] = str(exc)[:200]

    if report["db"] == "ok":
        try:
            report.update(
                {
                    "last_frontline_build": db.session.execute(
                        text("SELECT max(built_at) FROM frontline_snapshots")
                    ).scalar(),
                    "pending_llm": db.session.execute(
                        text("SELECT count(*) FROM news_items WHERE llm_status = 'pending'")
                    ).scalar(),
                    "degraded_sources": db.session.execute(
                        text("SELECT count(*) FROM sources WHERE status <> 'ok' AND enabled")
                    ).scalar(),
                    "enabled_sources": db.session.execute(
                        text("SELECT count(*) FROM sources WHERE enabled")
                    ).scalar(),
                    "news_items": db.session.execute(
                        text("SELECT count(*) FROM news_items")
                    ).scalar(),
                    "gazetteer_entries": db.session.execute(
                        text("SELECT count(*) FROM gazetteer")
                    ).scalar(),
                }
            )
        except Exception as exc:
            report["db"] = "degraded"
            report["db_error"] = str(exc)[:200]
            db.session.rollback()

    last_build = report.get("last_frontline_build")
    if last_build is not None and not isinstance(last_build, str):
        report["last_frontline_build"] = last_build.isoformat()
        report["last_frontline_build_human"] = last_build.strftime("%Y-%m-%d %H:%M UTC")
    return report


def dashboard_stats() -> dict:
    stats = read_stats(STAT_NAMES)
    rows = db.session.execute(
        text(
            """
            SELECT status, count(*) AS n FROM sources WHERE enabled GROUP BY status
            """
        )
    ).mappings().all()
    claims = db.session.execute(
        text("SELECT status, count(*) AS n FROM frontline_claims GROUP BY status")
    ).mappings().all()
    return {
        "counters": stats,
        "source_status": {r["status"]: r["n"] for r in rows},
        "claim_status": {r["status"]: r["n"] for r in claims},
        "health": health_report(),
    }
