"""Newest-first news feed with cursor pagination and blackout masking (PLAN §15)."""
from __future__ import annotations

from flask import request
from sqlalchemy import text

from app.api import bad_request, bp, cached_json
from app.config import settings
from app.extensions import db
from app.models import PERSPECTIVES

MAX_LIMIT = 100
SNIPPET_CHARS = 240


def _snippet(body: str | None) -> str | None:
    if not body:
        return None
    text_body = " ".join(body.split())
    return text_body if len(text_body) <= SNIPPET_CHARS else text_body[:SNIPPET_CHARS] + "…"


@bp.get("/news")
def news():
    try:
        cursor = request.args.get("cursor")
        cursor_id = int(cursor) if cursor else None
        limit = min(int(request.args.get("limit", 50)), MAX_LIMIT)
        source_id = request.args.get("source_id")
        source_id = int(source_id) if source_id else None
    except (TypeError, ValueError):
        return bad_request("cursor, limit and source_id must be integers")
    if limit < 1:
        return bad_request("limit must be positive")

    perspective = request.args.get("perspective")
    if perspective and perspective not in PERSPECTIVES:
        return bad_request(f"perspective must be one of {list(PERSPECTIVES)}")
    query = (request.args.get("q") or "").strip()
    if len(query) > 200:
        return bad_request("q too long")

    params = {
        "cursor_id": cursor_id,
        "limit": limit,
        "source_id": source_id,
        "perspective": perspective,
        "q": f"%{query}%" if query else None,
    }
    return cached_json("news", params, lambda: _build(params), ttl=settings.api_cache_ttl_s)


def _build(params: dict) -> dict:
    clauses = ["n.visible"]
    if params["cursor_id"]:
        clauses.append("n.id < :cursor_id")
    if params["source_id"]:
        clauses.append("n.source_id = :source_id")
    if params["perspective"]:
        clauses.append("s.perspective = :perspective")
    if params["q"]:
        clauses.append("(n.title ILIKE :q OR n.body ILIKE :q)")
    # Blackout rule: drop items whose *only* placed events sit inside an active zone.
    # Unplaced items always pass.
    clauses.append(
        """
        NOT (
            EXISTS (SELECT 1 FROM extracted_events e
                     WHERE e.news_item_id = n.id AND e.geom IS NOT NULL)
            AND NOT EXISTS (
                SELECT 1 FROM extracted_events e
                 WHERE e.news_item_id = n.id AND e.geom IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM blackout_zones b
                                    WHERE b.active AND ST_Intersects(b.geom, e.geom))
            )
        )
        """
    )
    where = " AND ".join(clauses)

    rows = db.session.execute(
        text(
            f"""
            SELECT n.id, n.title, n.title_en, n.body, n.body_en, n.translation_status,
                   n.url, n.published_at, n.fetched_at, n.llm_status,
                   s.id AS source_id, s.name AS source_name, s.perspective,
                   s.reliability_tier,
                   COALESCE(ev.events, '[]'::json) AS events
            FROM news_items n
            JOIN sources s ON s.id = n.source_id
            LEFT JOIN (
                SELECT e.news_item_id,
                       json_agg(json_build_object(
                           'id', e.id,
                           'event_type', e.event_type,
                           'placed', (e.geom IS NOT NULL),
                           'lat', ST_Y(e.geom), 'lon', ST_X(e.geom),
                           'place_name', e.place_name_raw,
                           'confidence', e.confidence
                       ) ORDER BY e.id) AS events
                FROM extracted_events e
                WHERE e.visible
                  AND (e.geom IS NULL OR NOT EXISTS (
                        SELECT 1 FROM blackout_zones b
                         WHERE b.active AND ST_Intersects(b.geom, e.geom)))
                GROUP BY e.news_item_id
            ) ev ON ev.news_item_id = n.id
            WHERE {where}
            ORDER BY n.id DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()

    items = [
        {
            "id": r["id"],
            # Display text prefers the English rendering; the original always travels with it so
            # the reader can switch back without another request.
            "title": r["title_en"] or r["title"],
            "title_original": r["title"],
            "snippet": _snippet(r["body_en"] or r["body"]),
            "snippet_original": _snippet(r["body"]),
            "translated": bool(r["title_en"]) and r["title_en"] != r["title"],
            "url": r["url"],
            "published_at": (r["published_at"] or r["fetched_at"]).isoformat(),
            "source": {
                "id": r["source_id"],
                "name": r["source_name"],
                "perspective": r["perspective"],
                "reliability_tier": r["reliability_tier"],
            },
            "events": r["events"],
        }
        for r in rows
    ]
    return {
        "items": items,
        "next_cursor": items[-1]["id"] if len(items) == params["limit"] else None,
    }
