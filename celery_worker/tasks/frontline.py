"""Upstream control-layer pulls and the frontline rebuild (PLAN §8.1-8.4, §14)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select, text

from app.config import settings
from app.extensions import db, log
from app.models import ExtractedEvent, NewsItem, Source, content_hash
from app.services.cache import bump_stat, frontline_is_dirty, invalidate, mark_frontline_dirty
from app.services.frontline import build, latest_upstream, store_upstream
from celery_worker.adapters import deepstate, geoconfirmed, isw_arcgis, warspotting
from celery_worker.celery_app import celery
from celery_worker.context import with_app_context


@celery.task(name="tasks.rebuild_frontline")
@with_app_context
def rebuild_frontline(force: bool = False) -> dict:
    if not force and not frontline_is_dirty():
        return {"status": "clean"}
    result = build(force=force)
    log.info("rebuild_frontline %s", result)
    return result


@celery.task(name="tasks.pull_deepstate")
@with_app_context
def pull_deepstate() -> dict:
    source = _api_source("deepstate")
    try:
        snapshot_id, classified, meta = deepstate.fetch_last()
    except Exception as exc:
        _fail(source, exc)
        return {"error": str(exc)[:200]}

    previous = latest_upstream("deepstate")
    if previous and previous.upstream_version == snapshot_id:
        _ok(source)
        return {"status": "unchanged", "snapshot_id": snapshot_id}

    # Stamp the snapshot's own publication time, so a live pull and a backfill of the same
    # snapshot land on the same instant.
    valid_at = classified.get("valid_at")
    try:
        store_upstream(
            "deepstate",
            classified["occupied"],
            snapshot_id,
            meta,
            plausible_km2=(deepstate.OCCUPIED_MIN_KM2, deepstate.OCCUPIED_MAX_KM2),
            valid_at=valid_at,
        )
    except ValueError as exc:
        # Implausible total: keep serving the last good snapshot rather than a broken map.
        _fail(source, exc)
        return {"error": str(exc)[:200]}

    # DeepState's own "unknown status" polygons are a first-class grey-zone input.
    if classified["grey"]:
        store_upstream(
            "deepstate_grey", classified["grey"], f"{snapshot_id}-grey", meta, valid_at=valid_at
        )

    if source:
        source.meta = {**(source.meta or {}), "last_snapshot_id": snapshot_id}
    _ok(source)
    mark_frontline_dirty()
    return {
        "status": "stored",
        "snapshot_id": snapshot_id,
        "occupied_polygons": meta["occupied_polygons"],
        "grey_polygons": meta["grey_polygons"],
        "dropped_by_token": meta["dropped_by_token"],
    }


@celery.task(name="tasks.pull_isw")
@with_app_context
def pull_isw() -> dict:
    source = _api_source("isw")
    service = (source.meta or {}).get("service") if source else None
    try:
        edit_date, geometries, meta = isw_arcgis.fetch_control(service)
    except Exception as exc:
        _fail(source, exc)
        return {"error": str(exc)[:200]}

    previous = latest_upstream("isw")
    if previous and previous.upstream_version == edit_date:
        _ok(source)
        return {"status": "unchanged", "edit_date": edit_date}

    store_upstream("isw", geometries, edit_date, meta)
    if source:
        source.meta = {**(source.meta or {}), "service": meta["service"], "last_editdate": edit_date}
    _ok(source)
    mark_frontline_dirty()
    return {"status": "stored", "edit_date": edit_date, "polygons": meta["polygons"]}


@celery.task(name="tasks.pull_geoconfirmed")
@with_app_context
def pull_geoconfirmed(days: int | None = None) -> dict:
    """Daily full-export pull, upserted as geolocation_proof events (PLAN §8.3)."""
    source = _api_source("geoconfirmed")
    if source is None:
        return {"error": "no geoconfirmed source configured"}
    window = days if days is not None else settings.geoconfirmed_backfill_days
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window)
    try:
        items = geoconfirmed.fetch_recent(since)
    except Exception as exc:
        _fail(source, exc)
        return {"error": str(exc)[:200]}

    inserted = _ingest_geolocated(source, items, "geolocation_proof")
    _ok(source)
    bump_stat("items_ingested", inserted)
    if inserted:
        invalidate("api:events")
        invalidate("api:news")
        mark_frontline_dirty()
    return {"fetched": len(items), "inserted": inserted, "since": since.date().isoformat()}


@celery.task(name="tasks.pull_warspotting")
@with_app_context
def pull_warspotting() -> dict:
    """Recent losses plus a 7-day re-poll, since per-date confirmation backfills late (§8.4)."""
    source = _api_source("warspotting")
    if source is None:
        return {"error": "no warspotting source configured"}
    try:
        items = warspotting.fetch_recent()
        hour = dt.datetime.now(dt.timezone.utc).hour
        if hour == 5:  # once a day, re-poll the trailing week
            items += warspotting.fetch_backfill()
    except Exception as exc:
        _fail(source, exc)
        return {"error": str(exc)[:200]}

    inserted = _ingest_geolocated(source, items, "other")
    _ok(source)
    bump_stat("items_ingested", inserted)
    if inserted:
        invalidate("api:events")
    return {"fetched": len(items), "inserted": inserted}


# --------------------------------------------------------------------------------------------
def _api_source(adapter: str) -> Source | None:
    return db.session.execute(
        select(Source).where(Source.type == "api", Source.meta["adapter"].as_string() == adapter)
    ).scalars().first()


def _ok(source: Source | None) -> None:
    if source is not None:
        source.consecutive_failures = 0
        source.status = "ok"
        source.last_polled_at = source.last_success_at = dt.datetime.now(dt.timezone.utc)
    db.session.commit()


def _fail(source: Source | None, exc: Exception) -> None:
    log.warning("upstream pull failed: %s: %s", type(exc).__name__, exc)
    if source is not None:
        source.consecutive_failures += 1
        source.last_polled_at = dt.datetime.now(dt.timezone.utc)
        if source.consecutive_failures >= 20:
            source.status = "dead"
        elif source.consecutive_failures >= 5:
            source.status = "degraded"
    db.session.commit()
    bump_stat("polls_failed")


def _ingest_geolocated(source: Source, items: list[dict], event_type: str) -> int:
    """Synthetic news_item + explicit-coordinate event per upstream record (PLAN §8.3)."""
    inserted = 0
    for item in items:
        digest = content_hash(item.get("title"), item.get("body"))
        exists = db.session.execute(
            select(NewsItem.id).where(NewsItem.content_hash == digest)
        ).scalar()
        if exists:
            continue
        try:
            with db.session.begin_nested():
                news = NewsItem(
                    source_id=source.id,
                    external_id=item.get("external_id"),
                    content_hash=digest,
                    title=item.get("title"),
                    body=item.get("body"),
                    url=item.get("url"),
                    published_at=item.get("published_at"),
                    llm_status="skipped",  # coordinates are already explicit: no LLM needed
                )
                db.session.add(news)
                db.session.flush()
                event = ExtractedEvent(
                    news_item_id=news.id,
                    event_type=event_type,
                    place_name_raw=None,
                    occurred_at=item.get("published_at") or dt.datetime.now(dt.timezone.utc),
                    coord_source="explicit_coords",
                    confidence=1.0 if event_type == "geolocation_proof" else 0.9,
                    llm_raw={"upstream": source.meta.get("adapter") if source.meta else None},
                )
                db.session.add(event)
                db.session.flush()
                db.session.execute(
                    text(
                        "UPDATE extracted_events "
                        "SET geom = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) WHERE id = :eid"
                    ),
                    {"lon": item["lon"], "lat": item["lat"], "eid": event.id},
                )
            inserted += 1
        except Exception as exc:
            log.debug("geolocated ingest skipped %s: %s", item.get("external_id"), exc)
    db.session.commit()
    return inserted
