"""Nightly housekeeping: snapshot pruning and a source-health rollup (PLAN §10)."""
from __future__ import annotations

from sqlalchemy import text

from app.config import settings
from app.extensions import db, log
from celery_worker.celery_app import celery
from celery_worker.context import with_app_context


@celery.task(name="tasks.maintenance")
@with_app_context
def maintenance() -> dict:
    # Thin out intra-day duplicates, keeping one snapshot per layer per day *of history*.
    #
    # This must partition on valid_at, not built_at. The live builder writes many snapshots a day,
    # all valid "now" — those are the duplicates worth collapsing. A backfill writes hundreds of
    # snapshots with distinct valid_at but a single built_at (today): partitioning on built_at put
    # them all in one bucket, so once that build date aged past the retention window the pruner
    # would have deleted all but one of them and silently destroyed the entire time-travel
    # history. Keyed on valid_at, each historical day keeps its snapshot indefinitely.
    pruned = db.session.execute(
        text(
            """
            DELETE FROM frontline_snapshots WHERE id IN (
                SELECT id FROM (
                    SELECT id, valid_at, row_number() OVER (
                        PARTITION BY layer, date_trunc('day', valid_at)
                        ORDER BY valid_at DESC, built_at DESC
                    ) AS rn
                    FROM frontline_snapshots
                ) ranked
                WHERE rn > 1 AND valid_at < now() - make_interval(days => :days)
            )
            """
        ),
        {"days": settings.snapshot_retention_days},
    ).rowcount

    upstream_pruned = db.session.execute(
        text(
            """
            DELETE FROM upstream_geometries WHERE id IN (
                SELECT id FROM (
                    SELECT id, row_number() OVER (
                        PARTITION BY provider ORDER BY fetched_at DESC
                    ) AS rn FROM upstream_geometries
                ) ranked WHERE rn > 30
            )
            """
        )
    ).rowcount

    audit_pruned = db.session.execute(
        text("DELETE FROM audit_log WHERE at < now() - interval '365 days'")
    ).rowcount

    # Health rollup: a source that has not succeeded in 20 poll intervals is degraded.
    rolled = db.session.execute(
        text(
            """
            UPDATE sources SET status = 'degraded'
            WHERE enabled AND status = 'ok' AND last_success_at IS NOT NULL
              AND last_success_at < now() - make_interval(secs => poll_interval_s * 20)
            """
        )
    ).rowcount

    # Release extraction claims abandoned by a crashed or killed worker.
    released = db.session.execute(
        text(
            "UPDATE news_items SET llm_status = 'pending', llm_claimed_at = NULL "
            "WHERE llm_status = 'processing' "
            "  AND (llm_claimed_at IS NULL "
            "       OR llm_claimed_at < now() - make_interval(mins => :mins))"
        ),
        {"mins": settings.llm_claim_stale_minutes},
    ).rowcount

    translations_released = db.session.execute(
        text(
            "UPDATE news_items SET translation_status = 'pending', translation_claimed_at = NULL "
            "WHERE translation_status = 'processing' "
            "  AND (translation_claimed_at IS NULL "
            "       OR translation_claimed_at < now() - make_interval(mins => :mins))"
        ),
        {"mins": settings.llm_claim_stale_minutes},
    ).rowcount

    db.session.commit()
    result = {
        "claims_released": released,
        "translations_released": translations_released,
        "snapshots_pruned": pruned,
        "upstream_pruned": upstream_pruned,
        "audit_pruned": audit_pruned,
        "sources_degraded": rolled,
    }
    log.info("maintenance %s", result)
    return result
