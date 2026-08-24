"""WarSpotting adapter — geolocated equipment losses, secondary corroboration (PLAN §8.4).

Per-date confirmation backfills with a lag, so ``recent`` is polled *and* the last 7 dates are
re-polled daily (a caveat recorded in sources/VERIFIED_ENDPOINTS.md).
"""
from __future__ import annotations

import datetime as dt

import orjson

from celery_worker.adapters.http import FetchError, fetch

BASE = "https://ukr.warspotting.net/api"
RECENT_URL = f"{BASE}/losses/{{side}}/recent/"
DATE_URL = f"{BASE}/losses/{{side}}/{{date}}/"
BACKFILL_DAYS = 7


def _parse(payload: bytes) -> list[dict]:
    try:
        data = orjson.loads(payload)
    except orjson.JSONDecodeError as exc:
        raise FetchError(f"warspotting: unparseable JSON ({exc})") from exc
    losses = data.get("losses")
    if not isinstance(losses, list):
        raise FetchError("warspotting: response has no 'losses' array")
    return losses


def to_item(loss: dict, side: str) -> dict | None:
    geo = (loss.get("geo") or "").strip()
    if "," not in geo:
        return None
    try:
        lat, lon = (float(part) for part in geo.split(",", 1))
    except ValueError:
        return None
    published = None
    if loss.get("date"):
        try:
            published = dt.datetime.strptime(loss["date"][:10], "%Y-%m-%d").replace(
                tzinfo=dt.timezone.utc
            )
        except ValueError:
            published = None
    tags = loss.get("tags")
    if isinstance(tags, str):          # the API returns a comma-joined string, not a list
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif not isinstance(tags, list):
        tags = []
    model = loss.get("model") or loss.get("type") or "equipment"
    status = loss.get("status") or "lost"
    where = loss.get("nearest_location") or ""
    unit = loss.get("unit") or ""
    return {
        "external_id": f"{side}:{loss.get('id')}",
        "title": f"{model} {status}{f' near {where}' if where else ''}"[:200],
        "body": " ".join(
            part
            for part in (
                f"{side} loss: {model} ({loss.get('type') or '—'}) {status}.",
                f"Nearest location: {where}." if where else "",
                f"Unit: {unit}." if unit else "",
                f"Tags: {', '.join(tags)}." if tags else "",
            )
            if part
        ),
        "url": f"https://ukr.warspotting.net/loss/{loss.get('id')}/" if loss.get("id") else None,
        "published_at": published,
        "lat": lat,
        "lon": lon,
    }


def fetch_recent(side: str = "russia") -> list[dict]:
    losses = _parse(fetch(RECENT_URL.format(side=side)).body)
    return [item for item in (to_item(l, side) for l in losses) if item]


def fetch_backfill(side: str = "russia", days: int = BACKFILL_DAYS) -> list[dict]:
    today = dt.datetime.now(dt.timezone.utc).date()
    out: list[dict] = []
    for offset in range(1, days + 1):
        date = (today - dt.timedelta(days=offset)).isoformat()
        try:
            losses = _parse(fetch(DATE_URL.format(side=side, date=date)).body)
        except FetchError:
            continue  # per-date endpoints are flaky; recent/ carries the same data eventually
        out.extend(item for item in (to_item(l, side) for l in losses) if item)
    return out
