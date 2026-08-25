"""LLM extraction task (PLAN §11): prefilter → batch → parse → extracted_events rows."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select, text

from app.config import settings
from app.extensions import db, log
from app.models import EVENT_TYPES, ExtractedEvent, NewsItem
from app.services import llm
from app.services.cache import bump_stat, invalidate
from app.services.geo import find_coordinates, in_ukraine_bbox
from celery_worker.celery_app import celery
from celery_worker.context import with_app_context

MAX_LLM_ATTEMPTS = 3  # PLAN §11: failed items retry at most twice more on later batches


def _claim(item_ids: list[int] | None) -> tuple[list[NewsItem], int]:
    """Atomically take ownership of the next items to extract.

    Returns (claimed batch, count prefiltered away). Row locks are taken with
    ``FOR UPDATE SKIP LOCKED`` so parallel workers glide past each other's rows instead of
    duplicating work, and the claim is committed before the (slow) proxy call so no lock is held
    across the network round trip.
    """
    # Lock ids only: NewsItem.source is an eager join, and Postgres rejects FOR UPDATE on the
    # nullable side of an outer join. Selecting the bare column keeps the statement join-free.
    id_query = select(NewsItem.id).with_for_update(skip_locked=True)
    if item_ids:
        id_query = id_query.where(NewsItem.id.in_(item_ids))
    else:
        id_query = (
            id_query.where(
                NewsItem.llm_status.in_(("pending", "failed")),
                NewsItem.llm_attempts < MAX_LLM_ATTEMPTS,
            )
            .order_by(NewsItem.llm_attempts, NewsItem.id.desc())
            .limit(settings.llm_batch_size * 3)
        )
    claimed_ids = db.session.execute(id_query).scalars().all()
    if not claimed_ids:
        return [], 0
    pending = (
        db.session.execute(
            select(NewsItem)
            .where(NewsItem.id.in_(claimed_ids))
            .order_by(NewsItem.llm_attempts, NewsItem.id.desc())
        )
        .scalars()
        .all()
    )

    skipped = 0
    candidates: list[NewsItem] = []
    for item in pending:
        # Prefilter first: items that cannot be about the war never reach the proxy.
        if llm.is_war_relevant(item.title, item.body):
            candidates.append(item)
        else:
            item.llm_status = "skipped"
            skipped += 1

    batch = candidates[: settings.llm_batch_size]
    claimed_at = dt.datetime.now(dt.timezone.utc)
    for item in batch:
        item.llm_status = "processing"
        item.llm_claimed_at = claimed_at
    db.session.commit()          # releases the row locks; the claim is now visible to everyone
    return batch, skipped


@celery.task(name="tasks.llm_extract_batch")
@with_app_context
def llm_extract_batch(item_ids: list[int] | None = None) -> dict:
    batch, skipped = _claim(item_ids)
    if not batch:
        return {"batch": 0, "skipped": skipped}

    payload = [
        {
            "idx": i,
            "source_name": item.source.name if item.source else "unknown",
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "title": llm.truncate(item.title),
            "body": llm.truncate(item.body),
        }
        for i, item in enumerate(batch)
    ]
    # LLM_BATCH_SIZE is an upper bound; the context window is the real constraint.
    payload = llm.fit_batch(payload)
    if len(payload) < len(batch):
        log.info("batch trimmed from %d to %d items to fit the context window",
                 len(batch), len(payload))
        for item in batch[len(payload):]:
            item.llm_status = "pending"      # release the claim on items that did not fit
            item.llm_claimed_at = None
        db.session.commit()
        batch = batch[: len(payload)]

    try:
        results = llm.extract_batch(payload)
    except Exception as exc:
        # Attempts exist to retire poison items, not to count outages: a transient failure
        # (proxy down/overloaded/unreachable) releases the claim without spending one, so the
        # backlog extracts itself once the endpoint is back.
        transient = llm.is_transient(exc)
        for item in batch:
            if transient:
                item.llm_status = "pending"
            else:
                item.llm_attempts += 1
                item.llm_status = "failed" if item.llm_attempts >= MAX_LLM_ATTEMPTS else "pending"
            item.llm_claimed_at = None
        db.session.commit()
        log.warning("extraction batch failed%s: %s: %s",
                    " (transient, no attempt spent)" if transient else "",
                    type(exc).__name__, exc)
        return {"batch": len(batch), "error": str(exc)[:200], "skipped": skipped}

    created = 0
    for idx, item in enumerate(batch):
        result = results.get(idx)
        item.llm_attempts += 1
        if result is None:
            item.llm_status = "failed" if item.llm_attempts >= MAX_LLM_ATTEMPTS else "pending"
            continue
        created += _persist(item, result)
        item.llm_status = "done"
    db.session.commit()

    bump_stat("events_created", created)
    if created:
        invalidate("api:events")
        invalidate("api:news")
    log.info("extracted batch=%d events=%d skipped=%d", len(batch), created, skipped)
    return {"batch": len(batch), "events": created, "skipped": skipped}


def _persist(item: NewsItem, result: dict) -> int:
    """Create extracted_events rows for one LLM result (PLAN §11 postprocess)."""
    if not result.get("relevant"):
        return 0
    event_type = result.get("event_type")
    if event_type not in EVENT_TYPES:
        event_type = "other"
    claimed_by = result.get("claimed_by")
    if claimed_by not in ("ru", "ua"):
        claimed_by = None
    try:
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError):
        confidence = None

    # Regex hits on the original text override LLM-reported coordinates (PLAN §11).
    regex_coords = [
        pair
        for pair in find_coordinates(f"{item.title or ''}\n{item.body or ''}")
        if in_ukraine_bbox(pair[0], pair[1])
    ]

    locations = result.get("locations")
    if not isinstance(locations, list):
        locations = []

    rows: list[tuple[str | None, float | None, float | None, str | None, str | None]] = []
    for i, location in enumerate(locations):
        if not isinstance(location, dict):
            continue
        name = (location.get("name") or "").strip() or None
        lat, lon = _coerce_coords(location.get("lat"), location.get("lon"))
        if regex_coords:
            # Prefer regex-extracted coordinates positionally; the LLM's own numbers are advisory.
            lat, lon = regex_coords[min(i, len(regex_coords) - 1)]
        if not in_ukraine_bbox(lat, lon):
            lat, lon = None, None  # outside the sanity bbox → treat as name-only
        rows.append((name, lat, lon, location.get("oblast"), _coerce_country(location.get("country"))))

    if not rows:
        if regex_coords:
            rows = [(None, regex_coords[0][0], regex_coords[0][1], None, None)]
        else:
            rows = [(None, None, None, None, None)]  # relevant but unplaced → feed-only item

    # The model routinely repeats a location within one response — one daily-summary item returned
    # the same empty location 18 times. Identical rows would become identical events: redundant on
    # the map and redundant as evidence on a claim.
    deduped: list[tuple[str | None, float | None, float | None, str | None, str | None]] = []
    seen_rows: set[tuple] = set()
    for row in rows:
        key = (
            (row[0] or "").strip().lower(),
            None if row[1] is None else round(row[1], 5),
            None if row[2] is None else round(row[2], 5),
        )
        if key in seen_rows:
            continue
        seen_rows.add(key)
        deduped.append(row)
    if len(deduped) < len(rows):
        log.debug("item %s: collapsed %d repeated locations", item.id, len(rows) - len(deduped))
    rows = deduped

    # The event's own time, not the ingest time — this is what the map scrubs along.
    occurred_at = item.published_at or item.fetched_at

    created = 0
    for name, lat, lon, oblast, country in rows:
        event = ExtractedEvent(
            news_item_id=item.id,
            event_type=event_type,
            place_name_raw=name,
            claimed_by=claimed_by,
            confidence=confidence,
            occurred_at=occurred_at,
            llm_raw={
                "event_type": event_type,
                "oblast": oblast,
                "country": country,
                "debunk_target": result.get("debunk_target"),
                "raw_confidence": result.get("confidence"),
            },
        )
        if lat is not None and lon is not None:
            event.coord_source = "explicit_coords"
        db.session.add(event)
        db.session.flush()
        if lat is not None and lon is not None:
            db.session.execute(
                text(
                    "UPDATE extracted_events SET geom = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) "
                    "WHERE id = :eid"
                ),
                {"lon": lon, "lat": lat, "eid": event.id},
            )
        created += 1
    return created


def _coerce_coords(lat, lon) -> tuple[float | None, float | None]:
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None


# The prompt asks for codes, but the model sometimes answers in words; both fold to the code.
# Anything unrecognized becomes None (= country unknown), never a guess.
_COUNTRY_CODES = {"ua", "ru", "by", "md", "other"}
_COUNTRY_WORDS = {"ukraine": "ua", "russia": "ru", "belarus": "by", "moldova": "md"}


def _coerce_country(value) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if key in _COUNTRY_CODES:
        return key
    return _COUNTRY_WORDS.get(key)
