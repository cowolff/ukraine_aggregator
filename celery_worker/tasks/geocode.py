"""Gazetteer geocoding task (PLAN §12)."""
from __future__ import annotations

from sqlalchemy import select, text

from app.extensions import db, log
from app.models import ExtractedEvent
from app.services.cache import invalidate
from app.services.geocode import match_place
from celery_worker.celery_app import celery
from celery_worker.context import with_app_context


@celery.task(name="tasks.geocode_pending")
@with_app_context
def geocode_pending(limit: int = 200) -> dict:
    events = (
        db.session.execute(
            select(ExtractedEvent)
            .where(
                ExtractedEvent.geom.is_(None),
                ExtractedEvent.place_name_raw.isnot(None),
                ExtractedEvent.gazetteer_id.is_(None),
            )
            .order_by(ExtractedEvent.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    placed = ambiguous = unmatched = 0
    for event in events:
        oblast_hint = (event.llm_raw or {}).get("oblast") if event.llm_raw else None
        match = match_place(event.place_name_raw, oblast_hint)
        if match is None:
            unmatched += 1
            continue
        if match.ambiguous:
            # Several equally-good candidates in different oblasts → stay unplaced (PLAN §12.3).
            ambiguous += 1
            continue
        db.session.execute(
            text(
                "UPDATE extracted_events SET geom = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), "
                "gazetteer_id = :gid, coord_source = 'gazetteer_match' WHERE id = :eid"
            ),
            {"lon": match.lon, "lat": match.lat, "gid": match.gazetteer_id, "eid": event.id},
        )
        placed += 1
    db.session.commit()
    if placed:
        invalidate("api:events")
        invalidate("api:news")
    log.info("geocoded placed=%d ambiguous=%d unmatched=%d", placed, ambiguous, unmatched)
    return {"placed": placed, "ambiguous": ambiguous, "unmatched": unmatched}
