"""ISW / Critical Threats ArcGIS adapter — Western assessed control (PLAN §8.2).

ISW renames services without notice, so the service name is re-resolved from the service
directory whenever the configured one fails.
"""
from __future__ import annotations

import orjson

from celery_worker.adapters.http import FetchError, fetch

BASE = "https://services5.arcgis.com/SaBe5HMtmnbqSWlu/arcgis/rest/services"
DEFAULT_SERVICE = "VIEW_RussiaCoTinUkraine_V3"
DEFAULT_LAYER = 49
# Fallback name patterns, most specific first, used when the configured service 404s.
NAME_PATTERNS = ("RussiaCoTinUkraine", "AssessedRussianControl", "RussianControlofTerrain")

QUERY_TMPL = (
    "{base}/{service}/FeatureServer/{layer}/query"
    "?where=1%3D1&outFields={fields}&returnGeometry={geom}&f={fmt}"
)


def _query_url(service: str, layer: int, *, fields="*", geometry=True, fmt="geojson") -> str:
    return QUERY_TMPL.format(
        base=BASE,
        service=service,
        layer=layer,
        fields=fields,
        geom="true" if geometry else "false",
        fmt=fmt,
    )


def list_services() -> list[str]:
    payload = orjson.loads(fetch(f"{BASE}?f=json").body)
    return [s.get("name", "") for s in payload.get("services", []) if s.get("name")]


def resolve_service(preferred: str = DEFAULT_SERVICE) -> str:
    """Confirm the preferred service exists, else pick the closest name match."""
    services = list_services()
    if preferred in services:
        return preferred
    for pattern in NAME_PATTERNS:
        for name in services:
            if pattern.lower() in name.lower():
                return name
    raise FetchError("isw: no assessed-control service found in the ArcGIS directory")


def current_editdate(service: str = DEFAULT_SERVICE, layer: int = DEFAULT_LAYER) -> int | None:
    """Cheap change detection: fetch EditDate only, no geometry (PLAN §8.2)."""
    url = _query_url(service, layer, fields="EditDate", geometry=False, fmt="json")
    payload = orjson.loads(fetch(url).body)
    if payload.get("error"):
        raise FetchError(f"isw: {payload['error'].get('message', 'query error')}")
    dates = [
        f.get("attributes", {}).get("EditDate")
        for f in payload.get("features", [])
        if f.get("attributes", {}).get("EditDate") is not None
    ]
    return max(dates) if dates else None


def polygon_geometries(feature_collection: dict) -> list[dict]:
    out = []
    for feature in (feature_collection or {}).get("features", []) or []:
        geometry = feature.get("geometry") or {}
        if geometry.get("type") in ("Polygon", "MultiPolygon"):
            out.append(geometry)
    return out


def fetch_control(service: str | None = None, layer: int = DEFAULT_LAYER):
    """Returns (edit_date, geometries, meta). Re-resolves the service name on failure."""
    service = service or DEFAULT_SERVICE
    try:
        edit_date = current_editdate(service, layer)
    except FetchError:
        service = resolve_service()
        edit_date = current_editdate(service, layer)

    payload = orjson.loads(fetch(_query_url(service, layer)).body)
    if payload.get("error"):
        raise FetchError(f"isw: {payload['error'].get('message', 'query error')}")
    geometries = polygon_geometries(payload)
    if not geometries:
        raise FetchError(f"isw: {service}/{layer} returned no polygons")
    return (
        str(edit_date) if edit_date is not None else None,
        geometries,
        {"provider": "isw", "service": service, "layer": layer, "polygons": len(geometries)},
    )
