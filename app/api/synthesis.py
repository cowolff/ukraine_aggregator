"""Synthesized cross-source reports for the general news rail (plans/SYNTHESIS.md §6).

A separate endpoint rather than a `placement` variant of /api/news: syntheses are rare by
construction (the source threshold), so the rail pins the top-K for the scrubber window instead
of interleaving them into the news cursor. Windowed on last_reported_at so time travel shows the
syntheses of that moment. The blackout mask applies like /api/events: a synthesis has a
geometry, so one whose centroid sits inside an active zone is withheld at publish time.
"""
from __future__ import annotations

from flask import request
from sqlalchemy import text

from app.api import bad_request, bp, cached_json
from app.api.events import parse_iso
from app.config import settings
from app.extensions import db

MAX_LIMIT = 50


@bp.get("/synthesis")
def synthesis():
    try:
        limit = min(int(request.args.get("limit", 20)), MAX_LIMIT)
    except (TypeError, ValueError):
        return bad_request("limit must be an integer")
    if limit < 1:
        return bad_request("limit must be positive")
    try:
        time_from = parse_iso(request.args.get("from"), "from")
        time_to = parse_iso(request.args.get("to"), "to")
    except ValueError as exc:
        return bad_request(str(exc))

    params = {"limit": limit, "time_from": time_from, "time_to": time_to}
    return cached_json("synthesis", params, lambda: _build(params), ttl=settings.api_cache_ttl_s)


def _build(params: dict) -> dict:
    clauses = ["r.visible", "r.llm_status = 'done'"]
    if params["time_from"]:
        clauses.append("r.last_reported_at >= :time_from")
    if params["time_to"]:
        clauses.append("r.last_reported_at <= :time_to")
    # Publish-time blackout mask, same contract as /api/events: ingestion and clustering keep
    # running behind an active zone; only the rendering is withheld.
    clauses.append(
        "NOT EXISTS (SELECT 1 FROM blackout_zones b "
        "WHERE b.active AND ST_Intersects(b.geom, r.geom))"
    )
    where = " AND ".join(clauses)

    rows = db.session.execute(
        text(
            f"""
            SELECT r.id, r.event_type, r.headline_en, r.summary_en, r.disagreements_en,
                   r.credibility, r.cred_meta, r.place_name, r.status,
                   ST_X(r.geom) AS lon, ST_Y(r.geom) AS lat,
                   r.first_reported_at, r.last_reported_at,
                   COALESCE((
                       SELECT json_agg(json_build_object(
                                  'news_item_id', t.nid,
                                  'source', t.source_name,
                                  'perspective', t.perspective,
                                  'tier', t.reliability_tier,
                                  'url', t.url,
                                  'title', t.title,
                                  'disputes', t.disputes
                              ) ORDER BY t.reliability_tier, t.nid)
                       FROM (
                           SELECT DISTINCT ON (n.id) n.id AS nid,
                                  s.name AS source_name, s.perspective, s.reliability_tier,
                                  n.url, COALESCE(n.title_en, n.title) AS title,
                                  bool_or(e.event_type = 'debunk')
                                      OVER (PARTITION BY n.id) AS disputes
                           FROM synthesis_members m
                           JOIN extracted_events e ON e.id = m.event_id
                           JOIN news_items n ON n.id = e.news_item_id
                           JOIN sources s ON s.id = n.source_id
                           WHERE m.report_id = r.id
                           ORDER BY n.id
                       ) t
                   ), '[]'::json) AS members
            FROM synthesized_reports r
            WHERE {where}
            ORDER BY r.last_reported_at DESC, r.id DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()

    items = [
        {
            "id": r["id"],
            "event_type": r["event_type"],
            "headline": r["headline_en"],
            "summary": r["summary_en"],
            "disagreements": r["disagreements_en"],
            "credibility": {"verdict": r["credibility"], **(r["cred_meta"] or {})},
            "place": r["place_name"],
            "centroid": [r["lon"], r["lat"]],
            "status": r["status"],
            "first_reported_at": r["first_reported_at"].isoformat(),
            "last_reported_at": r["last_reported_at"].isoformat(),
            "members": r["members"],
        }
        for r in rows
    ]
    return {"items": items}
