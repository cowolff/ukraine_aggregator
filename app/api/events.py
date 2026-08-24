"""GeoJSON event feed with bbox/time/type/perspective filters, clustering and blackout masking."""
from __future__ import annotations

import datetime as dt

from flask import request
from sqlalchemy import text

from app.api import bad_request, bp, cached_json
from app.config import settings
from app.extensions import db
from app.models import EVENT_TYPES, PERSPECTIVES
from app.services.geo import parse_bbox

MAX_LIMIT = 2000

# Cluster cell size in degrees per zoom hint — coarse near the world view, fine when zoomed in.
GRID_BY_ZOOM = {
    3: 1.0, 4: 0.6, 5: 0.4, 6: 0.25, 7: 0.15, 8: 0.08, 9: 0.05, 10: 0.03,
}


def parse_iso(raw: str | None, field: str) -> dt.datetime | None:
    if not raw:
        return None
    try:
        # A raw "+" in a query string arrives as a space, so "…T00:00:00 00:00" is a
        # legitimately-encoded offset. Accept it rather than 400-ing on it.
        value = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00").replace(" ", "+"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def _csv_set(raw: str | None, allowed: tuple[str, ...], field: str) -> list[str] | None:
    if not raw:
        return None
    values = [v.strip() for v in raw.split(",") if v.strip()]
    bad = [v for v in values if v not in allowed]
    if bad:
        raise ValueError(f"{field}: unknown value(s) {bad}")
    return values or None


def _default_window_start() -> dt.datetime:
    """Start of the default window, snapped down to a cache-TTL bucket.

    Without the snap, `from` would differ by microseconds on every request, so each poll would
    compute a fresh cache key and the Redis response cache (and client ETags) would never hit.
    """
    bucket = max(settings.api_cache_ttl_s, 1)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    snapped = dt.datetime.fromtimestamp(now - (now % bucket), tz=dt.timezone.utc)
    return snapped - dt.timedelta(hours=settings.events_default_window_h)


@bp.get("/events")
def events():
    try:
        bbox = parse_bbox(request.args.get("bbox"))
        time_from = parse_iso(request.args.get("from"), "from")
        time_to = parse_iso(request.args.get("to"), "to")
        types = _csv_set(request.args.get("types"), EVENT_TYPES, "types")
        perspectives = _csv_set(request.args.get("perspectives"), PERSPECTIVES, "perspectives")
        limit = min(int(request.args.get("limit", 1000)), MAX_LIMIT)
        zoom = int(float(request.args.get("zoom", 6)))
        # A reader filtering to their own shortlist needs individual points; a cluster cannot be
        # narrowed to a saved subset in the browser.
        allow_clusters = (request.args.get("cluster") or "").lower() not in ("off", "0", "false")
    except (ValueError, TypeError) as exc:
        return bad_request(str(exc))
    if limit < 1:
        return bad_request("limit must be positive")

    if time_from is None:
        time_from = _default_window_start()

    params = {
        "time_from": time_from,
        "time_to": time_to,
        "types": types,
        "perspectives": perspectives,
        "limit": limit,
    }
    cache_params = {
        "bbox": request.args.get("bbox"),
        "from": time_from.isoformat(),
        "to": time_to.isoformat() if time_to else None,
        "types": ",".join(types) if types else None,
        "perspectives": ",".join(perspectives) if perspectives else None,
        "limit": limit,
        "zoom": zoom,
        "cluster": allow_clusters,
    }

    return cached_json(
        "events",
        cache_params,
        lambda: _build(params, bbox, zoom, allow_clusters),
        ttl=settings.api_cache_ttl_s,
    )


def _where(bbox, params: dict) -> str:
    clauses = [
        "e.geom IS NOT NULL",
        "e.visible",
        "n.visible",
        # occurred_at, not created_at: a backfilled 2023 report was ingested today but belongs on
        # the map in 2023. created_at remains the ingest audit trail.
        "e.occurred_at >= :time_from",
        # Blackout masking is publish-time only; ingestion keeps running (PLAN §17.6).
        "NOT EXISTS (SELECT 1 FROM blackout_zones b WHERE b.active AND ST_Intersects(b.geom, e.geom))",
    ]
    if params.get("time_to"):
        clauses.append("e.occurred_at <= :time_to")
    if params.get("types"):
        clauses.append("e.event_type = ANY(:types)")
    if params.get("perspectives"):
        clauses.append("s.perspective = ANY(:perspectives)")
    if bbox:
        clauses.append("e.geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)")
        params.update(
            {
                "min_lon": bbox[0],
                "min_lat": bbox[1],
                "max_lon": bbox[2],
                "max_lat": bbox[3],
            }
        )
    return " AND ".join(clauses)


BASE_FROM = """
    FROM extracted_events e
    JOIN news_items n ON n.id = e.news_item_id
    JOIN sources s ON s.id = n.source_id
"""


def _build(params: dict, bbox, zoom: int, allow_clusters: bool = True) -> dict:
    where = _where(bbox, params)
    total = int(
        db.session.execute(text(f"SELECT count(*) {BASE_FROM} WHERE {where}"), params).scalar() or 0
    )

    if allow_clusters and total > settings.events_cluster_threshold:
        features = _clusters(where, params, zoom)
        clustered = True
    else:
        features = _points(where, params)
        clustered = False

    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "total": total,
            "clustered": clustered,
            "returned": len(features),
            "window_from": params["time_from"].isoformat(),
        },
    }


def _points(where: str, params: dict) -> list[dict]:
    rows = db.session.execute(
        text(
            f"""
            SELECT e.id, e.news_item_id, e.event_type, e.confidence, e.coord_source, e.claimed_by,
                   ST_X(e.geom) AS lon, ST_Y(e.geom) AS lat,
                   s.name AS source_name, s.perspective, s.reliability_tier,
                   n.title, n.title_en, n.url, n.published_at, e.created_at, e.occurred_at,
                   (SELECT l.claim_id FROM evidence_links l
                     WHERE l.event_id = e.id AND l.active ORDER BY l.id LIMIT 1) AS claim_id,
                   (SELECT c.status FROM evidence_links l
                      JOIN frontline_claims c ON c.id = l.claim_id
                     WHERE l.event_id = e.id AND l.active ORDER BY l.id LIMIT 1) AS claim_status
            {BASE_FROM}
            WHERE {where}
            ORDER BY e.occurred_at DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()

    return [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(r["lon"], 5), round(r["lat"], 5)]},
            "properties": {
                "id": r["id"],
                "news_item_id": r["news_item_id"],
                "event_type": r["event_type"],
                "perspective": r["perspective"],
                "source_name": r["source_name"],
                "reliability_tier": r["reliability_tier"],
                "title": r["title_en"] or r["title"],
                "title_original": r["title"],
                "translated": bool(r["title_en"]) and r["title_en"] != r["title"],
                "url": r["url"],
                "published_at": (r["published_at"] or r["occurred_at"]).isoformat(),
                "occurred_at": r["occurred_at"].isoformat(),
                "confidence": r["confidence"],
                "coord_source": r["coord_source"],
                "claimed_by": r["claimed_by"],
                # 'ua' | 'ru' | None — which side this event favours, or unassessed.
                "beneficiary": r["claimed_by"],
                "claim_id": r["claim_id"],
                "claim_status": r["claim_status"],
            },
        }
        for r in rows
    ]


# Pseudo-count for the lean estimate. A cluster holding a single Russian-perspective report is
# not evidence of one-sided coverage, but a raw ratio would paint it fully red. Adding k/2 to each
# side pulls small samples toward the neutral midpoint and lets large ones reach the poles:
# 1 report → 0.67, 20 reports → 0.95.
LEAN_PSEUDOCOUNT = 2.0


def side_lean(ukrainian: int, russian: int) -> float:
    """Share of a two-sided tally that falls on the Russian side, damped for small samples.

    0.0 = entirely Ukrainian, 1.0 = entirely Russian, 0.5 = balanced *or* nothing to weigh. Used
    for both colouring modes: "whose reporting is this" (perspective) and "who does this favour"
    (beneficiary). Anything off the axis — western/neutral sources, events with no beneficiary
    assessment — is excluded rather than folded in, so a cluster of wire copy reports as *no lean*
    instead of being misreported as balanced.
    """
    total = ukrainian + russian
    if total <= 0:
        return 0.5
    half = LEAN_PSEUDOCOUNT / 2
    return round((russian + half) / (total + LEAN_PSEUDOCOUNT), 4)


def _clusters(where: str, params: dict, zoom: int) -> list[dict]:
    grid = GRID_BY_ZOOM.get(max(3, min(zoom, 10)), 0.25)
    params = {**params, "grid": grid}
    rows = db.session.execute(
        text(
            f"""
            WITH matched AS (
                SELECT e.id, e.geom, e.event_type, e.claimed_by, s.perspective
                {BASE_FROM}
                WHERE {where}
            ), grouped AS (
                SELECT ST_SnapToGrid(geom, :grid) AS cell, count(*) AS n,
                       ST_Extent(geom) AS ext,
                       ST_X(ST_Centroid(ST_Collect(geom))) AS lon,
                       ST_Y(ST_Centroid(ST_Collect(geom))) AS lat,
                       count(*) FILTER (WHERE perspective = 'ukrainian') AS n_ukrainian,
                       count(*) FILTER (WHERE perspective = 'russian')   AS n_russian,
                       count(*) FILTER (WHERE perspective = 'western')   AS n_western,
                       count(*) FILTER (WHERE perspective = 'neutral')   AS n_neutral,
                       count(*) FILTER (WHERE claimed_by = 'ua')  AS b_ukrainian,
                       count(*) FILTER (WHERE claimed_by = 'ru')  AS b_russian,
                       count(*) FILTER (WHERE claimed_by IS NULL) AS b_unassessed
                FROM matched GROUP BY ST_SnapToGrid(geom, :grid)
            )
            SELECT n, lon, lat, n_ukrainian, n_russian, n_western, n_neutral,
                   b_ukrainian, b_russian, b_unassessed,
                   ST_XMin(ext) AS min_lon, ST_YMin(ext) AS min_lat,
                   ST_XMax(ext) AS max_lon, ST_YMax(ext) AS max_lat
            FROM grouped ORDER BY n DESC LIMIT :limit
            """
        ),
        params,
    ).mappings().all()

    out = []
    for r in rows:
        pad = grid / 4
        counts = {
            "ukrainian": int(r["n_ukrainian"]),
            "russian": int(r["n_russian"]),
            "western": int(r["n_western"]),
            "neutral": int(r["n_neutral"]),
        }
        # Who the reported events favour, as assessed at extraction time. Records ingested with
        # explicit coordinates and no LLM pass (GeoConfirmed's verification archive) carry no
        # such judgement and are counted as unassessed rather than guessed at.
        beneficiary = {
            "ukrainian": int(r["b_ukrainian"]),
            "russian": int(r["b_russian"]),
            "unassessed": int(r["b_unassessed"]),
        }
        out.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(r["lon"], 5), round(r["lat"], 5)]},
                "properties": {
                    "cluster": True,
                    "count": int(r["n"]),
                    "counts": counts,
                    "lean": side_lean(counts["ukrainian"], counts["russian"]),
                    "partisan": counts["ukrainian"] + counts["russian"],
                    "beneficiary_counts": beneficiary,
                    "lean_beneficiary": side_lean(
                        beneficiary["ukrainian"], beneficiary["russian"]
                    ),
                    "assessed": beneficiary["ukrainian"] + beneficiary["russian"],
                    "perspectives": sorted(k for k, v in counts.items() if v),
                    "expansion_bbox": [
                        round(r["min_lon"] - pad, 5),
                        round(r["min_lat"] - pad, 5),
                        round(r["max_lon"] + pad, 5),
                        round(r["max_lat"] + pad, 5),
                    ],
                },
            }
        )
    return out
