"""Newest-first news feed with cursor pagination and blackout masking (PLAN §15).

`placement` splits the corpus by whether the pipeline pinned the item to the map
(plans/NEWS_RAIL.md): an item is *placed* iff at least one of its visible extracted events has a
geometry. `from`/`to` window on COALESCE(published_at, fetched_at). `max_tier` keeps only
sources at or above that reliability (1 = most reliable).

Two orderings. The default, `order=id`, is ingest order with a plain `id` cursor — the original
contract, kept stable. `order=published` sorts by COALESCE(published_at, fetched_at) so the
latest *reporting* is first even when history is backfilled (backfill has large ids with old
dates); its cursor is a keyset pair `<id>@<iso timestamp>` issued in `next_cursor` and treated
as opaque by clients.
"""
from __future__ import annotations

from flask import request
from sqlalchemy import text

from app.api import bad_request, bp, cached_json
from app.api.events import parse_iso
from app.config import settings
from app.extensions import db
from app.models import PERSPECTIVES

MAX_LIMIT = 100
SNIPPET_CHARS = 240
PLACEMENTS = ("placed", "unplaced")
ORDERS = ("id", "published")
# reliability_tier is a small positive int, 1 = most reliable. The bound is deliberately loose:
# the catalogue currently uses 1–3, but the column carries no upper check constraint.
MAX_TIER_BOUND = 9


def _snippet(body: str | None) -> str | None:
    if not body:
        return None
    text_body = " ".join(body.split())
    return text_body if len(text_body) <= SNIPPET_CHARS else text_body[:SNIPPET_CHARS] + "…"


@bp.get("/news")
def news():
    order = request.args.get("order", "id")
    if order not in ORDERS:
        return bad_request(f"order must be one of {list(ORDERS)}")
    try:
        limit = min(int(request.args.get("limit", 50)), MAX_LIMIT)
        source_id = request.args.get("source_id")
        source_id = int(source_id) if source_id else None
        max_tier = request.args.get("max_tier")
        max_tier = int(max_tier) if max_tier else None
    except (TypeError, ValueError):
        return bad_request("limit, source_id and max_tier must be integers")
    if limit < 1:
        return bad_request("limit must be positive")
    if max_tier is not None and not 1 <= max_tier <= MAX_TIER_BOUND:
        return bad_request(f"max_tier must be between 1 and {MAX_TIER_BOUND}")

    # The cursor's shape follows the ordering: a plain id for order=id, an `<id>@<iso>` keyset
    # pair for order=published. Clients replay next_cursor verbatim, so mixing the two is a 400.
    cursor = request.args.get("cursor")
    cursor_id, cursor_ts = None, None
    if cursor:
        try:
            if order == "published":
                raw_id, sep, raw_ts = cursor.partition("@")
                if not sep:
                    raise ValueError
                cursor_id = int(raw_id)
                cursor_ts = parse_iso(raw_ts, "cursor")
            else:
                cursor_id = int(cursor)
        except (TypeError, ValueError):
            return bad_request("malformed cursor for this order")

    perspective = request.args.get("perspective")
    if perspective and perspective not in PERSPECTIVES:
        return bad_request(f"perspective must be one of {list(PERSPECTIVES)}")
    placement = request.args.get("placement")
    if placement and placement not in PLACEMENTS:
        return bad_request(f"placement must be one of {list(PLACEMENTS)}")
    try:
        time_from = parse_iso(request.args.get("from"), "from")
        time_to = parse_iso(request.args.get("to"), "to")
    except ValueError as exc:
        return bad_request(str(exc))
    query = (request.args.get("q") or "").strip()
    if len(query) > 200:
        return bad_request("q too long")

    params = {
        "order": order,
        "cursor_id": cursor_id,
        "cursor_ts": cursor_ts,
        "limit": limit,
        "source_id": source_id,
        "perspective": perspective,
        "placement": placement,
        "max_tier": max_tier,
        "time_from": time_from,
        "time_to": time_to,
        "q": f"%{query}%" if query else None,
    }
    return cached_json("news", params, lambda: _build(params), ttl=settings.api_cache_ttl_s)


def _build(params: dict) -> dict:
    clauses = ["n.visible"]
    # Relevance gate: extraction writes at least one event row — placed or not — for every item
    # it judges war-relevant (PLAN §11), so a judged item with zero rows is off-topic world news
    # from a mixed feed and never shown. The recall-heavy regex prefilter lets such items through
    # to the LLM on purpose; this is where the LLM's precision verdict reaches the reader.
    # Unjudged items (pending/processing/failed) stay visible until the verdict lands. Plain
    # EXISTS, not `visible`: an admin hiding an event moderates the map, not the item's relevance.
    clauses.append(
        "NOT (n.llm_status IN ('done','skipped') AND NOT EXISTS "
        "(SELECT 1 FROM extracted_events re WHERE re.news_item_id = n.id))"
    )
    if params["cursor_id"] and params["order"] == "published":
        # Keyset over (effective timestamp, id): Postgres row comparison keeps ties on the
        # timestamp stable by falling through to the id.
        clauses.append(
            "(COALESCE(n.published_at, n.fetched_at), n.id) < (:cursor_ts, :cursor_id)"
        )
    elif params["cursor_id"]:
        clauses.append("n.id < :cursor_id")
    if params["source_id"]:
        clauses.append("n.source_id = :source_id")
    if params["perspective"]:
        clauses.append("s.perspective = :perspective")
    if params["max_tier"]:
        clauses.append("s.reliability_tier <= :max_tier")
    if params["q"]:
        clauses.append("(n.title ILIKE :q OR n.body ILIKE :q)")
    # "Placed" = the pipeline pinned at least one visible event to a point. Blackout state is
    # deliberately not part of the predicate: a masked placed item *has* a location — it is
    # hidden by the blackout clause below, not reclassified as general news.
    placed = (
        "EXISTS (SELECT 1 FROM extracted_events pe "
        "WHERE pe.news_item_id = n.id AND pe.visible AND pe.geom IS NOT NULL)"
    )
    if params["placement"] == "placed":
        clauses.append(placed)
    elif params["placement"] == "unplaced":
        clauses.append(f"NOT {placed}")
    if params["time_from"]:
        clauses.append("COALESCE(n.published_at, n.fetched_at) >= :time_from")
    if params["time_to"]:
        clauses.append("COALESCE(n.published_at, n.fetched_at) <= :time_to")
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
    order_by = (
        "COALESCE(n.published_at, n.fetched_at) DESC, n.id DESC"
        if params["order"] == "published"
        else "n.id DESC"
    )

    rows = db.session.execute(
        text(
            f"""
            SELECT n.id, n.title, n.title_en, n.body, n.body_en, n.translation_status,
                   n.summary_en, n.url, n.published_at, n.fetched_at, n.llm_status,
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
                           'summary', e.summary_en,
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
            ORDER BY {order_by}
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
            # LLM-written general English summary; location-focused variants ride on the events.
            "summary": r["summary_en"],
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
    if len(items) < params["limit"]:
        next_cursor = None
    elif params["order"] == "published":
        # items[-1]["published_at"] is already the coalesced timestamp — the exact keyset value.
        next_cursor = f'{items[-1]["id"]}@{items[-1]["published_at"]}'
    else:
        next_cursor = items[-1]["id"]
    return {"items": items, "next_cursor": next_cursor}
