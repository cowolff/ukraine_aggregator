"""Geometry helpers shared by the API, the rule engine and the builder."""
from __future__ import annotations

import re

import orjson
from sqlalchemy import text

from app.config import settings
from app.extensions import db

DETAIL_TIERS = ("low", "mid", "high")
_COORD_PAIR = re.compile(
    r"(?<![\d.])(\d{1,2}\.\d{3,})\s*[,;/ ]\s*(\d{1,2}\.\d{3,})(?![\d.])"
)
_DMS = re.compile(
    r"(\d{1,2})[°º]\s*(\d{1,2})['′]\s*(\d{1,2}(?:\.\d+)?)?[\"″]?\s*([NS])"
    r"[,;\s]+(\d{1,3})[°º]\s*(\d{1,2})['′]\s*(\d{1,2}(?:\.\d+)?)?[\"″]?\s*([EW])",
    re.IGNORECASE,
)


def parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    """``minLon,minLat,maxLon,maxLat`` → tuple. Raises ValueError on malformed input."""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        raise ValueError("bbox needs 4 comma-separated numbers")
    min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("bbox min must be smaller than max")
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise ValueError("longitude out of range")
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise ValueError("latitude out of range")
    return min_lon, min_lat, max_lon, max_lat


def in_ukraine_bbox(lat: float | None, lon: float | None) -> bool:
    """Sanity gate for LLM/regex coordinates (PLAN §11): Ukraine plus border regions."""
    if lat is None or lon is None:
        return False
    lon_min, lat_min, lon_max, lat_max = settings.ukraine_bbox
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def find_coordinates(text_blob: str | None) -> list[tuple[float, float]]:
    """Regex-scan text for literal coordinates. Regex hits override LLM lat/lon (PLAN §11).

    Decimal pairs are assumed lat,lon — the only ordering used in practice by the Telegram/OSINT
    ecosystem — and are validated against the Ukraine bbox by the caller.
    """
    if not text_blob:
        return []
    found: list[tuple[float, float]] = []
    for match in _DMS.finditer(text_blob):
        lat_d, lat_m, lat_s, lat_h, lon_d, lon_m, lon_s, lon_h = match.groups()
        lat = int(lat_d) + int(lat_m) / 60 + float(lat_s or 0) / 3600
        lon = int(lon_d) + int(lon_m) / 60 + float(lon_s or 0) / 3600
        if lat_h.upper() == "S":
            lat = -lat
        if lon_h.upper() == "W":
            lon = -lon
        found.append((round(lat, 6), round(lon, 6)))
    for match in _COORD_PAIR.finditer(text_blob):
        a, b = float(match.group(1)), float(match.group(2))
        found.append((a, b))
    # de-duplicate, preserve order
    seen: set[tuple[float, float]] = set()
    out = []
    for pair in found:
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def simplify_to_geojson(wkb_expr: str, tolerance: float) -> str:
    return (
        f"ST_AsGeoJSON(ST_SimplifyPreserveTopology({wkb_expr}, {tolerance}), 6)"
    )


def blackout_union_sql() -> str:
    """SQL scalar subquery returning the union of active blackout polygons (or NULL)."""
    return "(SELECT ST_Union(geom) FROM blackout_zones WHERE active)"


def active_blackout_count() -> int:
    return int(db.session.execute(text("SELECT count(*) FROM blackout_zones WHERE active")).scalar() or 0)


def geojson_loads(raw: str | bytes | None):
    if raw is None:
        return None
    return orjson.loads(raw)


EMPTY_MULTIPOLYGON = {"type": "MultiPolygon", "coordinates": []}
