"""DeepStateMap adapter — Ukrainian-OSINT occupied-area polygons (PLAN §8.1).

The spec warns that "features include areas outside Ukraine context". In practice the feature
collection mixes four very different things, distinguished only by a token embedded in the
polygon's ``name``:

* ``geoJSON.status.occupied`` / ``territories.crimea`` / ``territories.ordlo``
      — currently Russian-held Ukrainian territory. This is the occupation layer.
* ``geoJSON.status.unknown``
      — DeepState's *own* declared grey zone. Used directly as grey-zone input, which is better
        evidence of unclear control than any derived geometry.
* ``geoJSON.status.dismissed`` / ``dismissed_at``
      — **liberated** territory. Unioning these into "occupied" inflates Russian control; when
        this was measured it added ~42,000 km² of Ukrainian-held land.
* ``geoJSON.territories.<elsewhere>``
      — Karelia, Kuril, Prussia, Abkhazia, Transnistria, Estonia, … DeepState's political
        commentary on Russian-held territory *outside* Ukraine, ~165,000 km² of it.

Filtering only by geometry type (the naive reading) therefore over-reports occupied area by
about 2.6×. With the classification below the occupied total lands within ~0.5% of ISW's
independently-assessed figure, which is what makes the two layers comparable at all.
"""
from __future__ import annotations

import collections
import datetime as dt
import re

import orjson

from celery_worker.adapters.http import FetchError, fetch

BASE = "https://deepstatemap.live/api"
LAST_URL = f"{BASE}/history/last"
PUBLIC_URL = f"{BASE}/history/public"
HISTORY_URL = f"{BASE}/history/{{id}}/geojson"

_TOKEN = re.compile(r"geoJSON\.(status|territories)\.([A-Za-z_]+)")

OCCUPIED_TOKENS = frozenset({"status.occupied", "territories.crimea", "territories.ordlo"})
GREY_TOKENS = frozenset({"status.unknown"})

# Plausibility band for the occupied total, in km². Guards against a silent upstream token
# rename zeroing (or exploding) the map: outside this band the pull fails soft and the last
# good snapshot keeps serving.
OCCUPIED_MIN_KM2 = 50_000
OCCUPIED_MAX_KM2 = 250_000


def classify(feature_collection: dict) -> dict:
    """Split the collection into occupied / grey geometries, with a tally of what was dropped."""
    occupied: list[dict] = []
    grey: list[dict] = []
    dropped: collections.Counter = collections.Counter()

    for feature in (feature_collection or {}).get("features", []) or []:
        geometry = feature.get("geometry") or {}
        gtype = geometry.get("type")
        if gtype not in ("Polygon", "MultiPolygon"):
            continue
        cleaned = {"type": gtype, "coordinates": _strip_z(geometry.get("coordinates"), gtype)}
        token = _token_of(feature)
        if token in OCCUPIED_TOKENS:
            occupied.append(cleaned)
        elif token in GREY_TOKENS:
            grey.append(cleaned)
        else:
            dropped[token] += 1

    return {"occupied": occupied, "grey": grey, "dropped": dict(dropped)}


def _token_of(feature: dict) -> str:
    name = ((feature.get("properties") or {}).get("name") or "").replace("\xa0", " ")
    match = _TOKEN.search(name)
    return f"{match.group(1)}.{match.group(2)}" if match else "untokenised"


def polygon_geometries(feature_collection: dict) -> list[dict]:
    """Occupied-area polygons only. Kept as the module's simple entry point."""
    return classify(feature_collection)["occupied"]


def _strip_z(coords, gtype: str):
    """DeepState publishes lon/lat/0 triples; PostGIS wants 2D."""
    if gtype == "Polygon":
        return [[[c[0], c[1]] for c in ring] for ring in coords or []]
    return [[[[c[0], c[1]] for c in ring] for ring in polygon] for polygon in coords or []]


def snapshot_instant(snapshot_id) -> dt.datetime | None:
    """DeepState's snapshot ids are unix timestamps of the moment the snapshot was published.

    Verified against the history index: id 1787509777 == 2026-08-23T18:29:37Z. Deriving the
    instant this way lets a live pull stamp the same ``valid_at`` a backfill would, so the two
    paths agree and re-running a backfill does not create near-duplicate history.
    """
    try:
        value = int(snapshot_id)
    except (TypeError, ValueError):
        return None
    # Sanity: within the plausible lifetime of this project rather than a random integer.
    if not 1_600_000_000 < value < 2_500_000_000:
        return None
    return dt.datetime.fromtimestamp(value, dt.timezone.utc)


def fetch_last() -> tuple[str | None, dict, dict]:
    """Returns (snapshot_id, {'occupied': [...], 'grey': [...]}, meta)."""
    result = fetch(LAST_URL)
    try:
        payload = orjson.loads(result.body)
    except orjson.JSONDecodeError as exc:
        raise FetchError(f"deepstate: unparseable JSON ({exc})") from exc

    snapshot_id = payload.get("id")
    classified = classify(payload.get("map") or {})
    if not classified["occupied"]:
        raise FetchError(
            "deepstate: no occupied polygons matched the status tokens — "
            f"upstream may have renamed them (dropped: {classified['dropped']})"
        )

    instant = snapshot_instant(snapshot_id)
    meta = {
        "provider": "deepstate",
        "snapshot_id": snapshot_id,
        "valid_at": instant.isoformat() if instant else None,
        "occupied_polygons": len(classified["occupied"]),
        "grey_polygons": len(classified["grey"]),
        "dropped_by_token": classified["dropped"],
    }
    classified["valid_at"] = instant
    return (str(snapshot_id) if snapshot_id is not None else None, classified, meta)


def fetch_history(limit: int = 20) -> list[dict]:
    """Recent change descriptions — usable as neutral-perspective news items."""
    records = history_index()
    return records[-limit:] if limit else records


def history_index() -> list[dict]:
    """Every published snapshot, oldest first, with a real timestamp attached.

    The ``datetime`` field is a display string with no year ("23.08 o 20:29"), so ``createdAt`` is
    the only usable instant. Records without one are dropped rather than guessed at.
    """
    records = orjson.loads(fetch(PUBLIC_URL).body)
    if not isinstance(records, list):
        return []

    out: list[dict] = []
    for record in records:
        stamp = record.get("createdAt") or record.get("updatedAt")
        if not stamp or record.get("id") is None:
            continue
        try:
            valid_at = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if valid_at.tzinfo is None:
            valid_at = valid_at.replace(tzinfo=dt.timezone.utc)
        out.append(
            {
                "id": record["id"],
                "valid_at": valid_at,
                "label": record.get("datetime"),
                "description": record.get("descriptionEn") or record.get("description"),
            }
        )
    out.sort(key=lambda r: r["valid_at"])
    return out


def fetch_snapshot(snapshot_id: int | str) -> dict:
    """Classified polygons for one historical snapshot (same shape as ``fetch_last``)."""
    result = fetch(HISTORY_URL.format(id=snapshot_id))
    try:
        payload = orjson.loads(result.body)
    except orjson.JSONDecodeError as exc:
        raise FetchError(f"deepstate: snapshot {snapshot_id} unparseable ({exc})") from exc
    # Historical snapshots are a bare FeatureCollection; /history/last wraps it under "map".
    collection = payload.get("map") if isinstance(payload, dict) and "map" in payload else payload
    classified = classify(collection or {})
    if not classified["occupied"]:
        raise FetchError(f"deepstate: snapshot {snapshot_id} had no occupied polygons")
    return classified
