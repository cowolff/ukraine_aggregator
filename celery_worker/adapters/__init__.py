"""Ingestion adapters. ``fetch_source_preview`` backs the admin "test fetch" button."""
from __future__ import annotations

from celery_worker.adapters import (  # noqa: F401
    deepstate,
    geoconfirmed,
    http,
    isw_arcgis,
    rss,
    telegram_web,
    warspotting,
)

API_ADAPTERS = {
    "deepstate": deepstate,
    "isw": isw_arcgis,
    "geoconfirmed": geoconfirmed,
    "warspotting": warspotting,
}


def fetch_source_preview(source) -> tuple[list[dict], str]:
    """Run a source's adapter once, without persisting anything (PLAN §17.2)."""
    if source.type == "rss":
        items, state = rss.poll(source)
        note = "304 Not Modified — feed unchanged" if state.get("not_modified") else ""
        return items, note
    if source.type == "telegram":
        items, state = telegram_web.poll(source)
        note = "page parsed but contained no new messages" if state.get("empty_page") else ""
        return items, note
    if source.type == "api":
        adapter = (source.meta or {}).get("adapter")
        if adapter == "deepstate":
            snapshot_id, geoms, meta = deepstate.fetch_last()
            return [
                {
                    "title": f"DeepStateMap snapshot {snapshot_id}",
                    "body": f"{meta['polygons']} occupied-area polygons",
                    "external_id": snapshot_id,
                    "published_at": None,
                    "url": deepstate.LAST_URL,
                }
            ], ""
        if adapter == "isw":
            edit_date, geoms, meta = isw_arcgis.fetch_control()
            return [
                {
                    "title": f"ISW assessed control, EditDate {edit_date}",
                    "body": f"{meta['polygons']} polygons from {meta['service']}/{meta['layer']}",
                    "external_id": edit_date,
                    "published_at": None,
                    "url": isw_arcgis.BASE,
                }
            ], ""
        if adapter == "warspotting":
            return warspotting.fetch_recent()[:3], ""
        if adapter == "geoconfirmed":
            import datetime as dt

            since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
            return geoconfirmed.fetch_recent(since)[:3], "(3-day slice of the full export)"
        return [], f"unknown api adapter {adapter!r} in source.meta"
    if source.type == "scrape":
        return [], "generic scrape sources have no adapter yet — use rss or telegram"
    return [], f"unsupported source type {source.type!r}"
