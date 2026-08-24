"""Public API: ETag/304, bbox and filter handling, blackout masking, cursor pagination (§15, §20)."""
from __future__ import annotations

import datetime as dt

import orjson
from sqlalchemy import text

from app.extensions import db
from app.services.frontline import build, store_upstream

DS_SQUARE = {"type": "Polygon", "coordinates": [[[37.0, 48.0], [38.0, 48.0], [38.0, 49.0], [37.0, 49.0], [37.0, 48.0]]]}
ISW_SQUARE = {"type": "Polygon", "coordinates": [[[37.5, 48.0], [38.5, 48.0], [38.5, 49.0], [37.5, 49.0], [37.5, 48.0]]]}


def add_blackout(client_app, wkt="POLYGON((37.5 48.5, 38.5 48.5, 38.5 49.5, 37.5 49.5, 37.5 48.5))"):
    db.session.execute(
        text(
            "INSERT INTO blackout_zones (name, geom, active) "
            "VALUES ('test zone', ST_GeomFromText(:wkt, 4326), true)"
        ),
        {"wkt": wkt},
    )
    db.session.commit()


class TestConfig:
    def test_config_exposes_client_contract(self, client):
        payload = client.get("/api/config").get_json()
        assert payload["poll_seconds"] > 0
        assert payload["map_defaults"]["center"] == [31.0, 48.5]
        assert "frontline_advance" in payload["event_types"]
        assert set(payload["colors"]) == {"ukrainian", "russian", "western", "neutral"}
        assert payload["glyphs"]["deep_strike"]


class TestFrontlineEndpoint:
    def test_empty_before_first_build(self, client):
        payload = client.get("/api/frontline").get_json()
        assert payload["built_at"] is None
        assert payload["layers"]["ru"] == {"type": "MultiPolygon", "coordinates": []}

    def test_serves_built_layers(self, client):
        store_upstream("deepstate", [DS_SQUARE], "ds-1", {})
        store_upstream("isw", [ISW_SQUARE], "isw-1", {})
        build(force=True)
        payload = client.get("/api/frontline?detail=mid").get_json()
        assert payload["built_at"]
        assert payload["layers"]["ru"]["coordinates"]
        assert payload["layers"]["grey"]["coordinates"]

    def test_bad_detail_is_a_400(self, client):
        resp = client.get("/api/frontline?detail=ultra")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "bad_request"

    def test_etag_then_304(self, client):
        store_upstream("deepstate", [DS_SQUARE], "ds-1", {})
        build(force=True)
        first = client.get("/api/frontline")
        etag = first.headers["ETag"]
        assert etag
        second = client.get("/api/frontline", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["ETag"] == etag

    def test_rebuild_invalidates_the_cache(self, client):
        store_upstream("deepstate", [DS_SQUARE], "ds-1", {})
        build(force=True)
        etag = client.get("/api/frontline").headers["ETag"]

        store_upstream("isw", [ISW_SQUARE], "isw-1", {})
        build(force=True)
        resp = client.get("/api/frontline", headers={"If-None-Match": etag})
        assert resp.status_code == 200, "a new snapshot must not answer 304 against the old ETag"


class TestEventsEndpoint:
    def test_geojson_shape_and_properties(self, client, make_source, make_event):
        source = make_source("russian", name="Rybar")
        make_event(source, lat=48.5, lon=37.5, event_type="frontline_advance")
        payload = client.get("/api/events").get_json()
        assert payload["type"] == "FeatureCollection"
        assert len(payload["features"]) == 1
        props = payload["features"][0]["properties"]
        assert props["perspective"] == "russian"
        assert props["source_name"] == "Rybar"
        assert props["event_type"] == "frontline_advance"
        assert "published_at" in props and "confidence" in props

    def test_bbox_filters(self, client, make_source, make_event):
        source = make_source("western")
        make_event(source, lat=48.5, lon=37.5)     # inside
        make_event(source, lat=51.0, lon=24.0)     # Lviv area, outside the bbox below
        inside = client.get("/api/events?bbox=37.0,48.0,38.0,49.0").get_json()
        assert len(inside["features"]) == 1

    def test_malformed_bbox_is_a_400(self, client):
        for bad in ("1,2,3", "a,b,c,d", "40,46,30,52"):
            assert client.get(f"/api/events?bbox={bad}").status_code == 400

    def test_type_and_perspective_filters(self, client, make_source, make_event):
        make_event(make_source("russian"), event_type="deep_strike", lat=48.5, lon=37.5)
        make_event(make_source("western"), event_type="shelling", lat=48.6, lon=37.6)

        only_strikes = client.get("/api/events?types=deep_strike").get_json()
        assert len(only_strikes["features"]) == 1
        assert only_strikes["features"][0]["properties"]["event_type"] == "deep_strike"

        only_western = client.get("/api/events?perspectives=western").get_json()
        assert len(only_western["features"]) == 1
        assert only_western["features"][0]["properties"]["perspective"] == "western"

    def test_unknown_filter_values_are_400(self, client):
        assert client.get("/api/events?types=nope").status_code == 400
        assert client.get("/api/events?perspectives=martian").status_code == 400
        assert client.get("/api/events?from=not-a-date").status_code == 400

    def test_time_window_uses_occurred_at_not_ingest_time(self, client, make_source, make_event):
        """A backfilled report belongs on the map when it happened, not when we ingested it."""
        from urllib.parse import quote

        source = make_source("western")
        long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
        event = make_event(source, lat=48.5, lon=37.5, occurred_at=long_ago)
        # Ingested just now, which the default 72h window must NOT rescue it by.
        db.session.execute(
            text("UPDATE extracted_events SET created_at = now() WHERE id=:i"), {"i": event.id}
        )
        db.session.commit()

        assert client.get("/api/events").get_json()["features"] == []

        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
        wide = client.get(f"/api/events?from={quote(since)}").get_json()
        assert len(wide["features"]) == 1
        assert wide["features"][0]["properties"]["occurred_at"].startswith(
            long_ago.date().isoformat()
        )

    def test_events_can_be_windowed_to_a_past_range(self, client, make_source, make_event):
        from urllib.parse import quote

        source = make_source("russian")
        now = dt.datetime.now(dt.timezone.utc)
        make_event(source, lat=48.5, lon=37.5, occurred_at=now - dt.timedelta(days=20))
        make_event(source, lat=48.6, lon=37.6, occurred_at=now - dt.timedelta(days=10))
        make_event(source, lat=48.7, lon=37.7, occurred_at=now - dt.timedelta(hours=1))

        start = quote((now - dt.timedelta(days=12)).isoformat())
        end = quote((now - dt.timedelta(days=8)).isoformat())
        window = client.get(f"/api/events?from={start}&to={end}").get_json()
        assert len(window["features"]) == 1, "only the event inside the window"

    def test_unplaced_events_never_appear_on_the_map(self, client, make_source, make_event):
        make_event(make_source("western"), lat=None, lon=None)
        assert client.get("/api/events").get_json()["features"] == []

    def test_hidden_event_is_masked(self, client, make_source, make_event):
        event = make_event(make_source("western"), lat=48.5, lon=37.5)
        db.session.execute(
            text("UPDATE extracted_events SET visible = false WHERE id = :i"), {"i": event.id}
        )
        db.session.commit()
        assert client.get("/api/events").get_json()["features"] == []

    def test_blackout_zone_masks_events(self, client, app, make_source, make_event):
        source = make_source("western")
        make_event(source, lat=48.7, lon=37.7)     # inside the zone
        make_event(source, lat=48.2, lon=37.2)     # outside it
        assert len(client.get("/api/events").get_json()["features"]) == 2

        add_blackout(app)
        from app.services.cache import invalidate

        invalidate("api:events")
        payload = client.get("/api/events").get_json()
        assert len(payload["features"]) == 1
        assert payload["features"][0]["geometry"]["coordinates"][1] < 48.5

    def test_deactivated_blackout_restores_history(self, client, app, make_source, make_event):
        make_event(make_source("western"), lat=48.7, lon=37.7)
        add_blackout(app)
        from app.services.cache import invalidate

        invalidate("api:events")
        assert client.get("/api/events").get_json()["features"] == []

        db.session.execute(text("UPDATE blackout_zones SET active = false"))
        db.session.commit()
        invalidate("api:events")
        assert len(client.get("/api/events").get_json()["features"]) == 1

    def test_clusters_when_over_the_threshold(self, client, app, make_source, make_event, monkeypatch):
        import dataclasses

        from app.config import settings

        # Settings is frozen, so swap in a modified copy where the endpoint reads it.
        monkeypatch.setattr(
            "app.api.events.settings",
            dataclasses.replace(settings, events_cluster_threshold=2),
        )
        source = make_source("russian")
        for i in range(4):
            make_event(source, lat=48.5 + i * 0.001, lon=37.5)
        payload = client.get("/api/events?zoom=6").get_json()
        assert payload["meta"]["clustered"] is True
        cluster = payload["features"][0]["properties"]
        assert cluster["cluster"] is True
        assert cluster["count"] == 4
        assert len(cluster["expansion_bbox"]) == 4
        assert cluster["counts"]["russian"] == 4
        assert cluster["lean"] > 0.5, "an all-Russian cluster leans red"

    def test_cluster_lean_reflects_the_perspective_mix(self, client, app, make_source,
                                                        make_event, monkeypatch):
        """Cluster colour must track who is reporting, not just that something is there."""
        import dataclasses

        from app.config import settings

        monkeypatch.setattr(
            "app.api.events.settings",
            dataclasses.replace(settings, events_cluster_threshold=2),
        )
        ru = make_source("russian")
        ua = make_source("ukrainian")
        for _ in range(6):
            make_event(ru, lat=48.5, lon=37.5)
        for _ in range(2):
            make_event(ua, lat=48.5 + 0.001, lon=37.5)

        payload = client.get("/api/events?zoom=6").get_json()
        cluster = payload["features"][0]["properties"]
        assert cluster["counts"] == {"ukrainian": 2, "russian": 6, "western": 0, "neutral": 0}
        assert cluster["partisan"] == 8
        assert 0.5 < cluster["lean"] < 1.0, "leans Russian without saturating"

    def test_beneficiary_axis_is_independent_of_perspective(self, client, app, make_source,
                                                            make_event, monkeypatch):
        """Ukrainian outlets reporting Russian gains: blue by perspective, red by beneficiary.

        This is the whole point of the second colouring mode — who reports something and who it
        favours are different questions and routinely disagree.
        """
        import dataclasses

        from app.config import settings

        monkeypatch.setattr(
            "app.api.events.settings",
            dataclasses.replace(settings, events_cluster_threshold=2),
        )
        ua_source = make_source("ukrainian")
        for _ in range(8):
            make_event(ua_source, lat=48.5, lon=37.5, claimed_by="ru")

        cluster = client.get("/api/events?zoom=6").get_json()["features"][0]["properties"]
        assert cluster["counts"]["ukrainian"] == 8, "reported entirely by Ukrainian sources"
        assert cluster["lean"] < 0.2, "perspective axis leans Ukrainian (blue)"
        assert cluster["beneficiary_counts"]["russian"] == 8
        assert cluster["lean_beneficiary"] > 0.8, "beneficiary axis leans Russian (red)"

    def test_unassessed_events_do_not_skew_the_beneficiary_axis(self, client, app, make_source,
                                                                make_event, monkeypatch):
        """Verification records carry no judgement of advantage and must not read as balanced."""
        import dataclasses

        from app.config import settings

        monkeypatch.setattr(
            "app.api.events.settings",
            dataclasses.replace(settings, events_cluster_threshold=2),
        )
        source = make_source("neutral")
        for _ in range(6):
            make_event(source, lat=48.5, lon=37.5, event_type="geolocation_proof",
                       claimed_by=None)

        cluster = client.get("/api/events?zoom=6").get_json()["features"][0]["properties"]
        assert cluster["beneficiary_counts"]["unassessed"] == 6
        assert cluster["assessed"] == 0
        assert cluster["lean_beneficiary"] == 0.5, "nothing to weigh → neutral midpoint"

    def test_point_features_expose_the_beneficiary(self, client, make_source, make_event):
        source = make_source("ukrainian")
        make_event(source, lat=48.5, lon=37.5, claimed_by="ru")
        make_event(source, lat=48.6, lon=37.6, claimed_by="ua")
        make_event(source, lat=48.7, lon=37.7, event_type="geolocation_proof", claimed_by=None)

        props = [f["properties"] for f in client.get("/api/events").get_json()["features"]]
        assert sorted(str(p["beneficiary"]) for p in props) == ["None", "ru", "ua"]

    def test_non_partisan_cluster_sits_at_the_neutral_midpoint(self, client, app, make_source,
                                                                make_event, monkeypatch):
        """Western/neutral wire copy is not on the Ukrainian-Russian axis."""
        import dataclasses

        from app.config import settings

        monkeypatch.setattr(
            "app.api.events.settings",
            dataclasses.replace(settings, events_cluster_threshold=2),
        )
        for _ in range(3):
            make_event(make_source("western"), lat=48.5, lon=37.5)
        for _ in range(3):
            make_event(make_source("neutral"), lat=48.5, lon=37.5)

        cluster = client.get("/api/events?zoom=6").get_json()["features"][0]["properties"]
        assert cluster["partisan"] == 0
        assert cluster["lean"] == 0.5, "no lean to show → the neutral midpoint"
        assert cluster["counts"]["western"] == 3 and cluster["counts"]["neutral"] == 3

    def test_events_etag_flow(self, client, make_source, make_event):
        make_event(make_source("western"), lat=48.5, lon=37.5)
        first = client.get("/api/events")
        assert client.get(
            "/api/events", headers={"If-None-Match": first.headers["ETag"]}
        ).status_code == 304


class TestNewsEndpoint:
    _seq = 0

    def _items(self, source, count, *, placed_lat=None, placed_lon=None):
        from app.models import ExtractedEvent, NewsItem, content_hash

        created = []
        for i in range(count):
            TestNewsEndpoint._seq += 1
            title = f"headline {i}"
            item = NewsItem(
                source_id=source.id,
                content_hash=content_hash(title, f"{source.id}-{i}-{TestNewsEndpoint._seq}"),
                title=title, body=f"body {i}", url=f"https://example.test/{i}",
                published_at=dt.datetime.now(dt.timezone.utc), llm_status="done",
            )
            db.session.add(item)
            db.session.flush()
            if placed_lat is not None:
                event = ExtractedEvent(
                    news_item_id=item.id, event_type="shelling",
                    coord_source="explicit_coords", confidence=0.8,
                )
                db.session.add(event)
                db.session.flush()
                db.session.execute(
                    text(
                        "UPDATE extracted_events SET geom = ST_SetSRID("
                        "ST_MakePoint(:lon,:lat),4326) WHERE id=:i"
                    ),
                    {"lon": placed_lon, "lat": placed_lat, "i": event.id},
                )
            created.append(item)
        db.session.commit()
        return created

    def test_newest_first_with_source_and_perspective(self, client, make_source):
        source = make_source("ukrainian", name="Ukrinform")
        self._items(source, 3)
        payload = client.get("/api/news").get_json()
        assert [i["title"] for i in payload["items"]] == ["headline 2", "headline 1", "headline 0"]
        assert payload["items"][0]["source"]["name"] == "Ukrinform"
        assert payload["items"][0]["source"]["perspective"] == "ukrainian"

    def test_cursor_pagination(self, client, make_source):
        source = make_source("western")
        self._items(source, 5)
        first = client.get("/api/news?limit=2").get_json()
        assert len(first["items"]) == 2
        assert first["next_cursor"] == first["items"][-1]["id"]

        second = client.get(f"/api/news?limit=2&cursor={first['next_cursor']}").get_json()
        assert len(second["items"]) == 2
        assert {i["id"] for i in first["items"]} & {i["id"] for i in second["items"]} == set()

        last = client.get(f"/api/news?limit=2&cursor={second['next_cursor']}").get_json()
        assert len(last["items"]) == 1
        assert last["next_cursor"] is None

    def test_search_and_perspective_filters(self, client, make_source):
        self._items(make_source("russian"), 2)
        self._items(make_source("western"), 2)
        assert len(client.get("/api/news?perspective=western").get_json()["items"]) == 2
        assert len(client.get("/api/news?q=headline 1").get_json()["items"]) == 2
        assert client.get("/api/news?q=nothingmatches").get_json()["items"] == []

    def test_bad_params_are_400(self, client):
        assert client.get("/api/news?cursor=abc").status_code == 400
        assert client.get("/api/news?perspective=martian").status_code == 400
        assert client.get("/api/news?limit=0").status_code == 400

    def test_events_are_attached_with_placement_flags(self, client, make_source):
        source = make_source("neutral")
        self._items(source, 1, placed_lat=48.5, placed_lon=37.5)
        item = client.get("/api/news").get_json()["items"][0]
        assert len(item["events"]) == 1
        assert item["events"][0]["placed"] is True
        assert item["events"][0]["lat"] == 48.5

    def test_blackout_hides_items_whose_only_events_are_inside(self, client, app, make_source):
        source = make_source("western")
        self._items(source, 1, placed_lat=48.7, placed_lon=37.7)   # inside the zone
        self._items(source, 1, placed_lat=48.2, placed_lon=37.2)   # outside
        assert len(client.get("/api/news").get_json()["items"]) == 2

        add_blackout(app)
        from app.services.cache import invalidate

        invalidate("api:news")
        items = client.get("/api/news").get_json()["items"]
        assert len(items) == 1

    def test_unplaced_items_always_pass_the_blackout(self, client, app, make_source):
        source = make_source("western")
        self._items(source, 2)          # no events at all
        add_blackout(app)
        from app.services.cache import invalidate

        invalidate("api:news")
        assert len(client.get("/api/news").get_json()["items"]) == 2

    def test_hidden_item_is_masked(self, client, make_source):
        source = make_source("western")
        items = self._items(source, 1)
        db.session.execute(
            text("UPDATE news_items SET visible = false WHERE id = :i"), {"i": items[0].id}
        )
        db.session.commit()
        assert client.get("/api/news").get_json()["items"] == []


class TestNotificationsAndHealth:
    def test_only_active_in_window_notifications_are_served(self, client):
        now = dt.datetime.now(dt.timezone.utc)
        rows = [
            ("live", True, None, None),
            ("inactive", False, None, None),
            ("expired", True, now - dt.timedelta(days=2), now - dt.timedelta(days=1)),
            ("future", True, now + dt.timedelta(days=1), None),
        ]
        for title, active, starts, ends in rows:
            db.session.execute(
                text(
                    "INSERT INTO notifications (title, level, active, starts_at, ends_at) "
                    "VALUES (:t, 'info', :a, :s, :e)"
                ),
                {"t": title, "a": active, "s": starts, "e": ends},
            )
        db.session.commit()
        titles = [n["title"] for n in client.get("/api/notifications").get_json()["items"]]
        assert titles == ["live"]

    def test_healthz_reports_infrastructure(self, client):
        payload = client.get("/healthz").get_json()
        assert payload["db"] == "ok"
        assert payload["redis"] == "ok"
        assert "pending_llm" in payload and "degraded_sources" in payload
        # No secrets may leak through an unauthenticated endpoint.
        blob = orjson.dumps(payload).decode().lower()
        assert "api_key" not in blob and "password" not in blob

    def test_index_is_served(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"maplibre" in resp.data.lower()
