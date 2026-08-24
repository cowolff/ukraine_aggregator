#!/usr/bin/env python
"""Rebuild the dataset from each source's own archive, stamped with when things happened.

The live pipeline only ever sees "now": pollers fetch what is currently published, and the map
shows the last few days. This script walks the archives the upstreams expose and writes them in
with their real timestamps, so the map can be scrubbed back by hour and date.

    python scripts/backfill_history.py --frontline --since 2024-01-01
    python scripts/backfill_history.py --geoconfirmed          # ~59k geolocations back to 2014
    python scripts/backfill_history.py --warspotting --days 365
    python scripts/backfill_history.py --telegram --days 90 --max-pages 40
    python scripts/backfill_history.py --all --since 2025-01-01

What each source offers:

* **DeepStateMap** — 1,736 published snapshots since 2022-04-03, one full frontline geometry each.
  This is the only true historical *control* layer available, so historical builds run in
  single-upstream mode (no ISW archive is ingested); each snapshot records that in
  ``generation_meta.degraded`` and its grey zone comes from DeepState's own "unknown status"
  polygons rather than from a cross-source disagreement.
* **GeoConfirmed** — the full CSV export is already historical: ~59k verified geolocations with
  dates back to 2014. No LLM needed, coordinates are explicit.
* **WarSpotting** — per-date endpoints going back years; roughly half of records are geolocated.
* **Telegram** — ``t.me/s/<slug>?before=<id>`` pages backwards ~20 messages at a time. These are
  free text, so they still have to go through extraction; the backfill only ingests them.

Everything here is idempotent: content hashes dedupe items, ``(provider, upstream_version)`` and
``(layer, valid_at)`` are unique, so a re-run resumes rather than duplicating.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text

from app import create_app
from app.extensions import db, log
from app.models import ExtractedEvent, NewsItem, Source, content_hash
from app.services.frontline import build, store_upstream
from celery_worker.adapters import deepstate, geoconfirmed, telegram_web, warspotting
from celery_worker.adapters.http import FetchError, fetch

UTC = dt.timezone.utc


def _parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value).replace(tzinfo=UTC)


def _api_source(adapter: str) -> Source | None:
    return db.session.execute(
        select(Source).where(Source.type == "api", Source.meta["adapter"].as_string() == adapter)
    ).scalars().first()


# --------------------------------------------------------------------------------------------
# frontline history
# --------------------------------------------------------------------------------------------
def backfill_frontline(
    since: dt.datetime | None,
    until: dt.datetime | None,
    per_day: int,
    pause: float,
    rebuild: bool,
) -> dict:
    index = deepstate.history_index()
    if since:
        index = [r for r in index if r["valid_at"] >= since]
    if until:
        index = [r for r in index if r["valid_at"] <= until]

    # Thin to at most `per_day` snapshots per calendar day, keeping the latest of each day.
    if per_day > 0:
        by_day: dict[dt.date, list[dict]] = {}
        for record in index:
            by_day.setdefault(record["valid_at"].date(), []).append(record)
        index = sorted(
            (r for day in by_day.values() for r in day[-per_day:]),
            key=lambda r: r["valid_at"],
        )

    existing = {
        row.upstream_version
        for row in db.session.execute(
            text("SELECT upstream_version FROM upstream_geometries WHERE provider = 'deepstate'")
        ).all()
    }
    print(f"deepstate history: {len(index)} snapshots selected, {len(existing)} already stored")

    stored = skipped = failed = built = 0
    for i, record in enumerate(index, 1):
        version = str(record["id"])
        if version in existing:
            skipped += 1
            continue
        try:
            classified = deepstate.fetch_snapshot(record["id"])
        except FetchError as exc:
            failed += 1
            print(f"  ! {version} @ {record['valid_at'].date()}: {exc}", file=sys.stderr)
            continue

        meta = {
            "provider": "deepstate",
            "snapshot_id": record["id"],
            "historical": True,
            "label": record.get("label"),
            "occupied_polygons": len(classified["occupied"]),
            "grey_polygons": len(classified["grey"]),
        }
        try:
            store_upstream(
                "deepstate", classified["occupied"], version, meta,
                plausible_km2=(deepstate.OCCUPIED_MIN_KM2, deepstate.OCCUPIED_MAX_KM2),
                valid_at=record["valid_at"],
            )
            if classified["grey"]:
                store_upstream(
                    "deepstate_grey", classified["grey"], f"{version}-grey", meta,
                    valid_at=record["valid_at"],
                )
            stored += 1
        except ValueError as exc:
            failed += 1
            print(f"  ! {version} @ {record['valid_at'].date()}: {exc}", file=sys.stderr)
            db.session.rollback()
            continue

        if rebuild:
            try:
                result = build(force=True, at=record["valid_at"])
                if result.get("status") == "ok":
                    built += 1
            except Exception as exc:
                log.warning("historical build failed for %s: %s", version, exc)
                db.session.rollback()

        if i % 25 == 0:
            print(f"  … {i}/{len(index)}  stored={stored} built={built} "
                  f"skipped={skipped} failed={failed}", flush=True)
        time.sleep(pause)

    return {"selected": len(index), "stored": stored, "built": built,
            "skipped": skipped, "failed": failed}


def rebuild_missing_snapshots() -> dict:
    """Build a frontline snapshot for every stored upstream instant that lacks one."""
    instants = [
        row.valid_at
        for row in db.session.execute(
            text(
                """
                SELECT DISTINCT u.valid_at FROM upstream_geometries u
                WHERE u.provider = 'deepstate'
                  AND NOT EXISTS (
                    SELECT 1 FROM frontline_snapshots f
                    WHERE f.layer = 'ru' AND f.valid_at = u.valid_at)
                ORDER BY u.valid_at
                """
            )
        ).all()
    ]
    print(f"rebuilding {len(instants)} missing snapshots")
    built = failed = 0
    for i, instant in enumerate(instants, 1):
        try:
            if build(force=True, at=instant).get("status") == "ok":
                built += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            log.warning("build at %s failed: %s", instant, exc)
            db.session.rollback()
        if i % 25 == 0:
            print(f"  … {i}/{len(instants)} built={built} failed={failed}", flush=True)
    return {"built": built, "failed": failed}


# --------------------------------------------------------------------------------------------
# event history
# --------------------------------------------------------------------------------------------
def _ingest_geolocated(source: Source, items: list[dict], event_type: str) -> int:
    """Insert already-geolocated upstream records, stamped with their own date."""
    inserted = 0
    for item in items:
        digest = content_hash(item.get("title"), item.get("body"))
        if db.session.execute(
            select(NewsItem.id).where(NewsItem.content_hash == digest)
        ).scalar():
            continue
        occurred = item.get("published_at") or dt.datetime.now(UTC)
        try:
            with db.session.begin_nested():
                news = NewsItem(
                    source_id=source.id, external_id=item.get("external_id"),
                    content_hash=digest, title=item.get("title"), body=item.get("body"),
                    url=item.get("url"), published_at=occurred,
                    llm_status="skipped",  # coordinates are explicit; no extraction needed
                )
                db.session.add(news)
                db.session.flush()
                event = ExtractedEvent(
                    news_item_id=news.id, event_type=event_type, place_name_raw=None,
                    coord_source="explicit_coords", occurred_at=occurred,
                    confidence=1.0 if event_type == "geolocation_proof" else 0.9,
                    llm_raw={"historical": True},
                )
                db.session.add(event)
                db.session.flush()
                db.session.execute(
                    text("UPDATE extracted_events SET geom = ST_SetSRID("
                         "ST_MakePoint(:lon, :lat), 4326) WHERE id = :eid"),
                    {"lon": item["lon"], "lat": item["lat"], "eid": event.id},
                )
            inserted += 1
        except Exception as exc:
            log.debug("skipped historical record %s: %s", item.get("external_id"), exc)
        if inserted and inserted % 2000 == 0:
            db.session.commit()
            print(f"  … {inserted} inserted", flush=True)
    db.session.commit()
    return inserted


def backfill_geoconfirmed(since: dt.datetime | None) -> dict:
    source = _api_source("geoconfirmed")
    if source is None:
        return {"error": "no geoconfirmed source configured"}
    items = geoconfirmed.fetch_recent(since)
    print(f"geoconfirmed: {len(items)} records with coordinates"
          f"{' since ' + since.date().isoformat() if since else ' (full archive)'}")
    return {"fetched": len(items), "inserted": _ingest_geolocated(source, items, "geolocation_proof")}


def backfill_warspotting(days: int, pause: float) -> dict:
    source = _api_source("warspotting")
    if source is None:
        return {"error": "no warspotting source configured"}
    today = dt.datetime.now(UTC).date()
    collected: list[dict] = []
    for offset in range(days):
        date = (today - dt.timedelta(days=offset)).isoformat()
        try:
            losses = warspotting._parse(
                fetch(warspotting.DATE_URL.format(side="russia", date=date)).body
            )
        except FetchError:
            continue
        collected.extend(i for i in (warspotting.to_item(l, "russia") for l in losses) if i)
        if offset and offset % 50 == 0:
            print(f"  … {offset}/{days} days, {len(collected)} geolocated losses", flush=True)
        time.sleep(pause)
    print(f"warspotting: {len(collected)} geolocated losses over {days} days")
    return {"fetched": len(collected), "inserted": _ingest_geolocated(source, collected, "other")}


def backfill_telegram(days: int, max_pages: int, pause: float, limit_sources: int | None) -> dict:
    """Page backwards through public channels. Text items still need LLM extraction afterwards."""
    horizon = dt.datetime.now(UTC) - dt.timedelta(days=days)
    query = select(Source).where(Source.type == "telegram", Source.enabled.is_(True))
    sources = db.session.execute(query.order_by(Source.reliability_tier, Source.id)).scalars().all()
    if limit_sources:
        sources = sources[:limit_sources]
    print(f"telegram: walking {len(sources)} channels back to {horizon.date()}")

    total_inserted = 0
    for source in sources:
        slug = (source.meta or {}).get("slug") or telegram_web.slug_from_url(source.url)
        if not slug:
            continue
        before: int | None = None
        inserted_here = 0
        for page in range(max_pages):
            url = f"https://t.me/s/{slug}" + (f"?before={before}" if before else "")
            try:
                result = fetch(url)
            except FetchError:
                break
            items = telegram_web.parse_page(result.text, slug)
            if not items:
                break
            numbers = [telegram_web._post_number(i["external_id"]) or 0 for i in items]
            before = min(n for n in numbers if n) if any(numbers) else None
            fresh = [i for i in items if i.get("published_at") and i["published_at"] >= horizon]
            inserted_here += _ingest_text_items(source, fresh)
            oldest = min((i["published_at"] for i in items if i.get("published_at")), default=None)
            if oldest is None or oldest < horizon or before is None:
                break
            time.sleep(pause)
        total_inserted += inserted_here
        print(f"  {source.name[:38]:40s} +{inserted_here}", flush=True)
    return {"inserted": total_inserted, "channels": len(sources)}


def _ingest_text_items(source: Source, items: list[dict]) -> int:
    inserted = 0
    for item in items:
        title = (item.get("title") or "").strip() or None
        body = (item.get("body") or "").strip() or None
        if not title and not body:
            continue
        digest = content_hash(title, body)
        if db.session.execute(
            select(NewsItem.id).where(NewsItem.content_hash == digest)
        ).scalar():
            continue
        try:
            with db.session.begin_nested():
                db.session.add(
                    NewsItem(
                        source_id=source.id, external_id=item.get("external_id"),
                        content_hash=digest, title=title, body=body, url=item.get("url"),
                        published_at=item.get("published_at"), llm_status="pending",
                    )
                )
            inserted += 1
        except Exception:
            pass
    db.session.commit()
    return inserted


# --------------------------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="run every backfill")
    parser.add_argument("--frontline", action="store_true", help="DeepStateMap snapshot history")
    parser.add_argument("--geoconfirmed", action="store_true", help="GeoConfirmed archive")
    parser.add_argument("--warspotting", action="store_true", help="WarSpotting per-date history")
    parser.add_argument("--telegram", action="store_true", help="page Telegram channels backwards")
    parser.add_argument("--rebuild-missing", action="store_true",
                        help="build snapshots for stored upstream instants that lack one")
    parser.add_argument("--since", help="ISO date, e.g. 2024-01-01")
    parser.add_argument("--until", help="ISO date")
    parser.add_argument("--days", type=int, default=180, help="lookback for warspotting/telegram")
    parser.add_argument("--per-day", type=int, default=1,
                        help="frontline snapshots to keep per day (0 = all)")
    parser.add_argument("--max-pages", type=int, default=30, help="telegram pages per channel")
    parser.add_argument("--max-channels", type=int, help="limit telegram channels (testing)")
    parser.add_argument("--pause", type=float, default=0.4, help="seconds between upstream calls")
    parser.add_argument("--no-rebuild", action="store_true",
                        help="store frontline history without building snapshots")
    args = parser.parse_args()

    if not any([args.all, args.frontline, args.geoconfirmed, args.warspotting, args.telegram,
                args.rebuild_missing]):
        parser.print_help()
        return 1

    since, until = _parse_date(args.since), _parse_date(args.until)
    app = create_app()
    with app.app_context():
        if args.all or args.frontline:
            print(backfill_frontline(since, until, args.per_day, args.pause,
                                     rebuild=not args.no_rebuild))
        if args.rebuild_missing:
            print(rebuild_missing_snapshots())
        if args.all or args.geoconfirmed:
            print(backfill_geoconfirmed(since))
        if args.all or args.warspotting:
            print(backfill_warspotting(args.days, args.pause))
        if args.all or args.telegram:
            print(backfill_telegram(args.days, args.max_pages, args.pause, args.max_channels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
