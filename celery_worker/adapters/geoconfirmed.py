"""GeoConfirmed adapter — volunteer-verified geolocations = the geolocation_proof feed (§8.3).

The CSV export is ~31 MB, semicolon-delimited, UTF-8 BOM. It is streamed to a temp file and
parsed row-by-row so a 4 GB node never holds the whole export in RAM.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import tempfile
from collections.abc import Iterator

from celery_worker.adapters.http import FetchError, stream_to_file

CSV_URL = "https://geoconfirmed.org/api/Map/export/Ukraine/csv"
DETAIL_URL = "https://geoconfirmed.org/api/Placemark/detail/{id}"
EXPECTED_COLUMNS = {"Date", "Latitude", "Longitude", "Description", "Source", "Id"}


def parse_csv_rows(handle) -> Iterator[dict]:
    reader = csv.DictReader(handle, delimiter=";")
    if not reader.fieldnames or not EXPECTED_COLUMNS.issubset({f.strip() for f in reader.fieldnames}):
        raise FetchError(f"geoconfirmed: unexpected CSV columns {reader.fieldnames}")
    for row in reader:
        yield {(k or "").strip(): (v or "").strip() for k, v in row.items()}


def to_item(row: dict) -> dict | None:
    """Map one CSV row to an ingestible item; None when it has no usable coordinates."""
    try:
        lat = float(row.get("Latitude") or "")
        lon = float(row.get("Longitude") or "")
    except ValueError:
        return None
    published = _parse_date(row.get("Date"))
    name = row.get("Name") or ""
    description = row.get("Description") or ""
    parts = [p for p in (name, description) if p]
    return {
        "external_id": row.get("Id") or None,
        "title": (name or description or "GeoConfirmed placemark")[:200],
        "body": "\n".join(parts),
        "url": row.get("Source") or row.get("Geolocation") or None,
        "published_at": published,
        "lat": lat,
        "lon": lon,
        "faction": row.get("Faction") or None,
        "equipment": row.get("Equipment") or None,
        "units": row.get("Units") or None,
    }


def _parse_date(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(raw[:19], fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_recent(since: dt.datetime | None) -> list[dict]:
    """Download the export and return items dated at/after ``since`` (all of them if None)."""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="geoconfirmed-")
        os.close(fd)
        size = stream_to_file(CSV_URL, tmp_path)
        if size < 1000:
            raise FetchError(f"geoconfirmed: export suspiciously small ({size} bytes)")
        out: list[dict] = []
        with open(tmp_path, encoding="utf-8-sig", newline="") as handle:
            for row in parse_csv_rows(handle):
                item = to_item(row)
                if item is None:
                    continue
                if since and item["published_at"] and item["published_at"] < since:
                    continue
                out.append(item)
        return out
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
