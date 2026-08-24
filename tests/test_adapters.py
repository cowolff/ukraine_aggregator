"""Adapters parse the recorded real-world fixtures (PLAN §20 unit tier)."""
from __future__ import annotations

import datetime as dt
import re

import orjson
import pytest

from celery_worker.adapters import deepstate, geoconfirmed, isw_arcgis, rss, telegram_web, warspotting
from tests.conftest import FIXTURES


def test_rss_parses_real_feed():
    items = rss.parse_feed((FIXTURES / "rss_feed.xml").read_bytes())
    assert len(items) >= 5
    first = items[0]
    assert first["title"] and "<" not in first["title"]
    assert first["url"].startswith("https://")
    assert isinstance(first["published_at"], dt.datetime)
    assert first["published_at"].tzinfo is not None
    assert first["external_id"]


def test_rss_strips_markup_and_entities():
    assert rss.strip_html("<p>Hit <b>near</b> Pokrovsk</p><script>evil()</script>") == "Hit near Pokrovsk"
    assert rss.strip_html("a &amp; b &quot;c&quot;") == 'a & b "c"'
    assert rss.strip_html(None) == ""


def test_telegram_parses_real_channel_page():
    html = (FIXTURES / "telegram_channel.html").read_text()
    items = telegram_web.parse_page(html)
    assert len(items) >= 5
    for item in items:
        assert item["body"]
        assert item["external_id"] and "/" in item["external_id"]
        assert item["url"].startswith("https://t.me/")
    numbers = [telegram_web._post_number(i["external_id"]) for i in items]
    assert all(n is not None for n in numbers)


def test_telegram_markup_drift_yields_nothing_not_an_exception():
    # A 200 page whose markup changed must parse to [] so the poller can mark it degraded.
    assert telegram_web.parse_page("<html><body><div class='other'>hi</div></body></html>") == []


@pytest.mark.parametrize(
    "body,expected",
    [
        # Decorative emoji opener: the headline is on a later line.
        ("⚡️\nTwo Majors\n#Summary\nfor the morning of August 24", "Two Majors #Summary for the morning of August 24"),
        ("🟠\nRussian Drone Strikes Petrol Station in Korop", "Russian Drone Strikes Petrol Station in Korop"),
        ("⚡️\nЗБИТО 191 ЦІЛЬ ПРОТИВНИКА\nУ ніч на 22 серпня", "ЗБИТО 191 ЦІЛЬ ПРОТИВНИКА"),
        # Already fine: left alone.
        ("A normal single-line post about Pokrovsk", "A normal single-line post about Pokrovsk"),
        # Nothing substantive at all: return what there is rather than an empty title.
        ("🔥🔥🔥", "🔥🔥🔥"),
        ("", ""),
    ],
)
def test_telegram_headline_skips_decorative_lines(body, expected):
    assert telegram_web.headline(body) == expected


def test_telegram_parsed_titles_are_not_bare_emoji():
    html = (FIXTURES / "telegram_channel.html").read_text()
    for item in telegram_web.parse_page(html):
        assert re.search(r"[^\W_]", item["title"], re.UNICODE), (
            f"title {item['title']!r} carries no words"
        )


def test_telegram_slug_extraction():
    assert telegram_web.slug_from_url("https://t.me/s/ClashReport") == "ClashReport"
    assert telegram_web.slug_from_url("https://t.me/war_mapper") == "war_mapper"
    assert telegram_web.slug_from_url("https://example.com/nope") is None


def test_deepstate_classifies_by_status_token():
    """Occupied, declared-grey, liberated and out-of-Ukraine polygons must be told apart.

    Treating every polygon as "occupied" (the naive reading of §8.1) over-reports Russian control
    by ~2.6×: it folds in liberated territory and DeepState's commentary polygons for Karelia,
    Transnistria and friends.
    """
    payload = orjson.loads((FIXTURES / "deepstate_last.json").read_bytes())
    result = deepstate.classify(payload["map"])

    # status.occupied + territories.crimea + territories.ordlo
    assert len(result["occupied"]) == 3
    # status.unknown — DeepState's own grey zone
    assert len(result["grey"]) == 1
    # liberated + the two non-Ukraine territories
    assert result["dropped"]["status.dismissed"] == 1
    assert result["dropped"]["territories.karelia"] == 1
    assert result["dropped"]["territories.transnistria"] == 1


def test_deepstate_liberated_areas_are_never_occupied():
    payload = orjson.loads((FIXTURES / "deepstate_last.json").read_bytes())
    occupied = deepstate.polygon_geometries(payload["map"])
    grey = deepstate.classify(payload["map"])["grey"]
    assert len(occupied) == 3
    assert occupied != grey


def test_deepstate_strips_the_z_ordinate():
    payload = orjson.loads((FIXTURES / "deepstate_last.json").read_bytes())
    for geometry in deepstate.polygon_geometries(payload["map"]):
        assert geometry["type"] in ("Polygon", "MultiPolygon")
        ring = (
            geometry["coordinates"][0]
            if geometry["type"] == "Polygon"
            else geometry["coordinates"][0][0]
        )
        assert all(len(coord) == 2 for coord in ring), "z ordinate must be stripped"


def test_deepstate_ignores_non_polygon_features():
    collection = {
        "type": "FeatureCollection",
        "features": [
            {"properties": {"name": "x /// geoJSON.status.occupied"},
             "geometry": {"type": "Point", "coordinates": [37.0, 48.0, 0]}},
            {"properties": {"name": "x /// geoJSON.status.occupied"},
             "geometry": {"type": "LineString", "coordinates": [[37, 48, 0], [38, 49, 0]]}},
            {"properties": {"name": "x /// geoJSON.status.occupied"},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[37, 48, 0], [38, 48, 0], [38, 49, 0], [37, 48, 0]]]}},
        ],
    }
    assert len(deepstate.polygon_geometries(collection)) == 1


def test_deepstate_untokenised_polygons_are_not_treated_as_occupied():
    collection = {
        "type": "FeatureCollection",
        "features": [
            {"properties": {"name": "mystery shape"},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[37, 48], [38, 48], [38, 49], [37, 48]]]}},
        ],
    }
    result = deepstate.classify(collection)
    assert result["occupied"] == []
    assert result["dropped"]["untokenised"] == 1


def test_deepstate_snapshot_id_is_its_publication_instant():
    # Cross-checked against the upstream history index, which reports this id as that timestamp.
    assert deepstate.snapshot_instant(1787509777).isoformat() == "2026-08-23T18:29:37+00:00"
    assert deepstate.snapshot_instant(1648989208).isoformat() == "2022-04-03T12:33:28+00:00"
    # Anything that is not a plausible timestamp is refused rather than mapped to 1970.
    assert deepstate.snapshot_instant(42) is None
    assert deepstate.snapshot_instant("not-a-number") is None
    assert deepstate.snapshot_instant(None) is None


def test_deepstate_fetch_raises_when_no_occupied_polygons_match(monkeypatch):
    """An upstream token rename must fail loudly, not silently empty the map."""
    from celery_worker.adapters.http import Fetched

    body = orjson.dumps({
        "id": 1,
        "map": {"type": "FeatureCollection", "features": [
            {"properties": {"name": "renamed /// geoJSON.status.somethingelse"},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[37, 48], [38, 48], [38, 49], [37, 48]]]}},
        ]},
    })
    monkeypatch.setattr(deepstate, "fetch", lambda url, **kw: Fetched(200, body))
    with pytest.raises(Exception, match="no occupied polygons"):
        deepstate.fetch_last()


def test_isw_geojson_polygons():
    payload = orjson.loads((FIXTURES / "isw_control.geojson").read_bytes())
    geometries = isw_arcgis.polygon_geometries(payload)
    assert geometries
    assert all(g["type"] in ("Polygon", "MultiPolygon") for g in geometries)


def test_warspotting_maps_geo_string_to_coordinates():
    # Roughly half of WarSpotting's records carry no `geo` at all; those are dropped, not faked.
    payload = orjson.loads((FIXTURES / "warspotting_recent.json").read_bytes())
    raw = payload["losses"]
    items = [i for i in (warspotting.to_item(loss, "russia") for loss in raw) if i]
    assert items
    assert len(items) == sum(1 for loss in raw if loss.get("geo"))
    for item in items:
        assert 40 < item["lat"] < 56 and 20 < item["lon"] < 45
        assert item["external_id"].startswith("russia:")
        assert item["title"]


def test_warspotting_tags_as_string_or_list():
    base = {"id": 1, "model": "T-72", "status": "Destroyed", "geo": "48.1,37.2", "date": "2026-08-01"}
    as_string = warspotting.to_item({**base, "tags": "Cope cage,ERA"}, "russia")
    as_list = warspotting.to_item({**base, "tags": ["Cope cage", "ERA"]}, "russia")
    assert "Cope cage, ERA" in as_string["body"]
    assert "Cope cage, ERA" in as_list["body"]


def test_warspotting_skips_records_without_coordinates():
    assert warspotting.to_item({"id": 2, "geo": ""}, "russia") is None
    assert warspotting.to_item({"id": 3, "geo": "not,coords"}, "russia") is None


def test_geoconfirmed_csv_is_semicolon_delimited_with_bom():
    path = FIXTURES / "geoconfirmed_head.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(geoconfirmed.parse_csv_rows(handle))
    assert rows
    items = [geoconfirmed.to_item(row) for row in rows]
    placed = [i for i in items if i]
    assert placed, "at least one row must carry usable coordinates"
    for item in placed:
        assert item["external_id"]
        assert -90 <= item["lat"] <= 90 and -180 <= item["lon"] <= 180


def test_geoconfirmed_rejects_unexpected_columns():
    import io

    from celery_worker.adapters.http import FetchError

    with pytest.raises(FetchError):
        list(geoconfirmed.parse_csv_rows(io.StringIO("a;b;c\n1;2;3\n")))


def test_geoconfirmed_date_formats():
    assert geoconfirmed._parse_date("2026-08-19").year == 2026
    assert geoconfirmed._parse_date("19-08-2026").month == 8
    assert geoconfirmed._parse_date("") is None
    assert geoconfirmed._parse_date("nonsense") is None
