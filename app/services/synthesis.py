"""Cross-source synthesis: clustering, credibility and arming (plans/SYNTHESIS.md).

The clustering mirrors the rules engine's incremental idiom (`nearby_claim`): each new placed
event either *joins* the nearest open report of its type within SYNTH_JOIN_KM or *founds* one.
Founding is silent — nothing renders until the cluster crosses SYNTH_MIN_SOURCES distinct
sources, which arms the LLM synthesis stage.

The credibility verdict is computed here, in code, from the member sources' reliability tiers
and perspective classes — never by the LLM (the same principle as perspective labelling). The
false-corroboration guards are inherited from the rules engine: digest suppression, the
confidence floor, distinct-source counting, and debunks/geolocations joining but never founding.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text

from app.config import settings
from app.extensions import db, log
from app.models import ExtractedEvent, SynthesisMember, SynthesizedReport
from app.services.cache import invalidate
from app.services.rules import PERSPECTIVE_CLASS, is_digest

# Evidence-only types: they join a nearby cluster (of any type) but never found one — a lone
# geolocation is the claims machinery's business, and a debunk with nothing to debunk is noise.
EVIDENCE_ONLY_TYPES = ("geolocation_proof", "debunk")

# Closed reports that never armed are singleton/duo clusters that never became a story. They are
# pruned once safely past the candidate window so their events cannot become candidates again.
PRUNE_AFTER_DAYS = 7


@dataclass
class ClusterResult:
    events_seen: int = 0
    joined: int = 0
    founded: int = 0
    closed: int = 0
    armed: int = 0
    refreshed: int = 0
    digests_skipped: int = 0
    pruned: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _window_minutes() -> int:
    return int(settings.synth_window_hours * 60)


# --------------------------------------------------------------------------------------------
# credibility (plans/SYNTHESIS.md §4) — deterministic, tier- and perspective-weighted
# --------------------------------------------------------------------------------------------
def _member_sources(report_id: int) -> list[dict]:
    """One row per distinct member *source*: perspective, tier, and what it contributed."""
    rows = db.session.execute(
        text(
            """
            SELECT s.id AS source_id, s.perspective, s.reliability_tier,
                   bool_or(e.event_type = 'geolocation_proof') AS has_geoproof,
                   bool_or(e.event_type = 'debunk') AS has_debunk,
                   bool_or(e.event_type NOT IN ('debunk')) AS reports_it
            FROM synthesis_members m
            JOIN extracted_events e ON e.id = m.event_id
            JOIN news_items n ON n.id = e.news_item_id
            JOIN sources s ON s.id = n.source_id
            WHERE m.report_id = :rid
            GROUP BY s.id, s.perspective, s.reliability_tier
            """
        ),
        {"rid": report_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def compute_credibility(source_rows: list[dict]) -> tuple[str, dict]:
    """The verdict ladder, evaluated top-down over distinct *reporting* sources.

    A source whose only contribution is a debunk disputes the story rather than reporting it, so
    it counts toward `disputed` but not toward the corroboration arithmetic.
    """
    reporters = [r for r in source_rows if r["reports_it"]]
    disputed = any(r["has_debunk"] for r in source_rows)

    classes = {PERSPECTIVE_CLASS.get(r["perspective"], "western") for r in reporters}
    tiers = sorted(int(r["reliability_tier"]) for r in reporters)
    tier_histogram: dict[str, int] = {}
    for tier in tiers:
        tier_histogram[str(tier)] = tier_histogram.get(str(tier), 0) + 1
    best_tier = tiers[0] if tiers else None
    classes_with_solid_tier = {
        PERSPECTIVE_CLASS.get(r["perspective"], "western")
        for r in reporters
        if int(r["reliability_tier"]) <= 2
    }
    tier1_geoproof = any(r["has_geoproof"] and int(r["reliability_tier"]) == 1 for r in reporters)

    meta = {
        "sources": len(reporters),
        "classes": sorted(classes),
        "best_tier": best_tier,
        "tiers": tier_histogram,
        "disputed": disputed,
    }

    if disputed:
        return "unverified", meta
    if tier1_geoproof or len(classes_with_solid_tier) >= 2:
        return "confirmed", meta
    if len(classes) >= 2 or (len(reporters) >= 3 and best_tier == 1):
        return "corroborated", meta
    if best_tier is not None and best_tier <= 2:
        return "reported", meta
    return "unverified", meta


# --------------------------------------------------------------------------------------------
# clustering
# --------------------------------------------------------------------------------------------
def _close_expired(now: dt.datetime, result: ClusterResult) -> None:
    """Expired open reports stop accepting members; ones with unsynthesized members get a final
    LLM pass so the stored summary reflects the complete evidence."""
    expired = (
        db.session.execute(
            text(
                """
                SELECT id, llm_status, member_count, synthesized_member_count
                FROM synthesized_reports
                WHERE status = 'open'
                  AND last_reported_at < :now - make_interval(mins => :win)
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"now": now, "win": _window_minutes()},
        )
        .mappings()
        .all()
    )
    for row in expired:
        report = db.session.get(SynthesizedReport, row["id"])
        report.status = "closed"
        report.updated_at = now
        if row["llm_status"] == "done" and row["member_count"] > row["synthesized_member_count"]:
            report.llm_status = "pending"
            result.refreshed += 1
        result.closed += 1


def _candidate_events(now: dt.datetime, limit: int) -> list[ExtractedEvent]:
    ids = [
        r.id
        for r in db.session.execute(
            text(
                """
                SELECT e.id
                FROM extracted_events e
                JOIN news_items n ON n.id = e.news_item_id
                WHERE e.geom IS NOT NULL
                  AND e.visible AND n.visible
                  AND e.occurred_at > :now - make_interval(mins => :win)
                  AND (e.confidence IS NULL OR e.confidence >= :floor)
                  AND NOT EXISTS (SELECT 1 FROM synthesis_members m WHERE m.event_id = e.id)
                ORDER BY e.id
                LIMIT :limit
                """
            ),
            {
                "now": now,
                "win": _window_minutes(),
                "floor": settings.low_confidence_floor,
                "limit": limit,
            },
        ).all()
    ]
    if not ids:
        return []
    return (
        db.session.query(ExtractedEvent)
        .filter(ExtractedEvent.id.in_(ids))
        .order_by(ExtractedEvent.id)
        .all()
    )


def _distinct_place_counts(news_item_ids: list[int]) -> dict[int, int]:
    """Distinct placed locations per item, all event types — the synthesis digest test.

    Broader than the rules engine's claimable-only count on purpose: an overnight-strikes
    roundup naming ten cities is a digest even though none of its events are claimable.
    """
    if not news_item_ids:
        return {}
    rows = db.session.execute(
        text(
            """
            SELECT news_item_id,
                   count(DISTINCT COALESCE(gazetteer_id::text, lower(place_name_raw),
                                           id::text)) AS n
            FROM extracted_events
            WHERE news_item_id = ANY(:ids) AND geom IS NOT NULL
            GROUP BY news_item_id
            """
        ),
        {"ids": list(set(news_item_ids))},
    ).all()
    return {r.news_item_id: int(r.n) for r in rows}


def _joinable_report_id(event: ExtractedEvent) -> int | None:
    """Nearest open report the event may join: same type (evidence-only types join any type),
    within SYNTH_JOIN_KM, and inside the report's join window."""
    type_clause = "" if event.event_type in EVIDENCE_ONLY_TYPES else "AND r.event_type = e.event_type"
    row = db.session.execute(
        text(
            f"""
            SELECT r.id
            FROM synthesized_reports r, extracted_events e
            WHERE e.id = :eid
              AND r.status = 'open'
              {type_clause}
              AND ST_DWithin(r.geom::geography, e.geom::geography, :radius)
              AND e.occurred_at >= r.first_reported_at - make_interval(mins => :win)
              AND e.occurred_at <= r.last_reported_at + make_interval(mins => :win)
            ORDER BY ST_Distance(r.geom::geography, e.geom::geography)
            LIMIT 1
            """
        ),
        {
            "eid": event.id,
            "radius": settings.synth_join_km * 1000.0,
            "win": _window_minutes(),
        },
    ).first()
    return row.id if row else None


def _found_report(event: ExtractedEvent) -> SynthesizedReport:
    report = SynthesizedReport(
        event_type=event.event_type,
        geom=event.geom,
        gazetteer_id=event.gazetteer_id,
        first_reported_at=event.occurred_at,
        last_reported_at=event.occurred_at,
        member_count=0,  # the recompute pass fills counts, centroid and place name
    )
    db.session.add(report)
    db.session.flush()
    return report


def _recompute(report_id: int, now: dt.datetime, debunk_joined: bool, result: ClusterResult) -> None:
    """Refresh a touched report's derived state: centroid, window, counts, place, credibility —
    and arm or re-arm the LLM stage."""
    report = db.session.get(SynthesizedReport, report_id)
    stats = db.session.execute(
        text(
            """
            SELECT count(*) AS member_count,
                   min(e.occurred_at) AS first_at,
                   max(e.occurred_at) AS last_at,
                   -- EWKT, not raw geometry: geoalchemy2 binds strings via ST_GeomFromEWKT.
                   ST_AsEWKT(ST_Centroid(ST_Collect(e.geom))) AS centroid
            FROM synthesis_members m
            JOIN extracted_events e ON e.id = m.event_id
            WHERE m.report_id = :rid
            """
        ),
        {"rid": report_id},
    ).first()
    place = db.session.execute(
        text(
            """
            SELECT COALESCE(g.name_en, e.place_name_raw) AS name, e.gazetteer_id
            FROM synthesis_members m
            JOIN extracted_events e ON e.id = m.event_id
            LEFT JOIN gazetteer g ON g.id = e.gazetteer_id
            WHERE m.report_id = :rid AND COALESCE(g.name_en, e.place_name_raw) IS NOT NULL
            GROUP BY 1, 2
            ORDER BY count(*) DESC, name
            LIMIT 1
            """
        ),
        {"rid": report_id},
    ).first()

    report.member_count = int(stats.member_count)
    report.first_reported_at = stats.first_at
    report.last_reported_at = stats.last_at
    report.geom = stats.centroid
    if place:
        report.place_name = place.name
        report.gazetteer_id = place.gazetteer_id
    report.updated_at = now

    verdict, meta = compute_credibility(_member_sources(report_id))
    report.credibility = verdict
    report.cred_meta = meta

    if report.llm_status is None and meta["sources"] >= settings.synth_min_sources:
        report.llm_status = "pending"
        result.armed += 1
    elif report.llm_status == "done" and (
        debunk_joined
        or report.member_count - report.synthesized_member_count >= settings.synth_refresh_min_new
    ):
        report.llm_status = "pending"
        result.refreshed += 1


def _prune(now: dt.datetime, result: ClusterResult) -> None:
    result.pruned = db.session.execute(
        text(
            """
            DELETE FROM synthesized_reports
            WHERE status = 'closed' AND llm_status IS NULL
              AND last_reported_at < :now - make_interval(days => :days)
            """
        ),
        {"now": now, "days": PRUNE_AFTER_DAYS},
    ).rowcount


def cluster_events(limit: int = 500) -> ClusterResult:
    result = ClusterResult()
    now = dt.datetime.now(dt.timezone.utc)

    _close_expired(now, result)

    events = _candidate_events(now, limit)
    # Evidence-only types go last so a debunk older than the reports it disputes can join the
    # cluster those reports found *in this same pass* instead of waiting for the next tick.
    events.sort(key=lambda e: (e.event_type in EVIDENCE_ONLY_TYPES, e.id))
    place_counts = _distinct_place_counts([e.news_item_id for e in events])
    # report_id -> did this pass add a debunk member to it
    touched: dict[int, bool] = {}

    for event in events:
        result.events_seen += 1
        if is_digest(place_counts.get(event.news_item_id)):
            # A roundup naming many settlements manufactures false corroboration: two General
            # Staff digests both mentioning Kupiansk are not two reports of one Kupiansk event.
            result.digests_skipped += 1
            continue
        report_id = _joinable_report_id(event)
        if report_id is None:
            if event.event_type in EVIDENCE_ONLY_TYPES:
                continue
            report_id = _found_report(event).id
            result.founded += 1
        else:
            result.joined += 1
        db.session.add(SynthesisMember(report_id=report_id, event_id=event.id))
        db.session.flush()
        touched[report_id] = touched.get(report_id, False) or event.event_type == "debunk"

    for report_id, debunk_joined in touched.items():
        _recompute(report_id, now, debunk_joined, result)

    _prune(now, result)
    db.session.commit()

    if result.closed or touched:
        # `status` and the credibility badge are part of the public payload.
        invalidate("api:synthesis")
    if result.founded or result.joined or result.armed or result.refreshed:
        log.info("synthesis clustering %s", result.as_dict())
    return result
