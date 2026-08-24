"""Going back by hour and date: historical builds, ?at= lookups and the timeline bounds."""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from sqlalchemy import text

from app.extensions import db
from app.services.frontline import build, latest_snapshots, latest_upstream, store_upstream

UTC = dt.timezone.utc

SMALL = {"type": "Polygon", "coordinates": [[[37.0, 48.0], [37.5, 48.0], [37.5, 48.5], [37.0, 48.5], [37.0, 48.0]]]}
BIG = {"type": "Polygon", "coordinates": [[[37.0, 48.0], [38.5, 48.0], [38.5, 49.0], [37.0, 49.0], [37.0, 48.0]]]}


def area_at(instant: dt.datetime | None) -> float:
    payload = latest_snapshots("high", instant)
    geometry = payload["layers"]["ru"]
    if not geometry.get("coordinates"):
        return 0.0
    return float(
        db.session.execute(
            text("SELECT ST_Area(ST_GeomFromGeoJSON(:g)::geography)/1e6"),
            {"g": __import__("orjson").dumps(geometry).decode()},
        ).scalar()
    )


class TestHistoricalBuilds:
    def test_upstream_is_selected_by_valid_at(self):
        old = dt.datetime(2024, 1, 1, tzinfo=UTC)
        new = dt.datetime(2025, 1, 1, tzinfo=UTC)
        store_upstream("deepstate", [SMALL], "v-old", {}, valid_at=old)
        store_upstream("deepstate", [BIG], "v-new", {}, valid_at=new)

        assert latest_upstream("deepstate").upstream_version == "v-new"
        assert latest_upstream("deepstate", old).upstream_version == "v-old"
        assert latest_upstream("deepstate", new).upstream_version == "v-new"
        # Before any data exists there is nothing to serve.
        assert latest_upstream("deepstate", dt.datetime(2023, 1, 1, tzinfo=UTC)) is None

    def test_a_historical_build_is_stamped_with_the_instant_it_depicts(self):
        instant = dt.datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        store_upstream("deepstate", [SMALL], "v1", {}, valid_at=instant)
        result = build(force=True, at=instant)
        assert result["status"] == "ok"
        assert result["valid_at"].startswith("2024-06-01")

        row = db.session.execute(
            text("SELECT valid_at, built_at FROM frontline_snapshots WHERE layer='ru'")
        ).mappings().first()
        assert row["valid_at"] == instant
        assert row["built_at"] > instant, "built now, valid then"

    def test_the_map_changes_as_you_scrub(self):
        early = dt.datetime(2024, 1, 1, tzinfo=UTC)
        late = dt.datetime(2024, 6, 1, tzinfo=UTC)
        store_upstream("deepstate", [SMALL], "v1", {}, valid_at=early)
        build(force=True, at=early)
        store_upstream("deepstate", [BIG], "v2", {}, valid_at=late)
        build(force=True, at=late)

        early_area, late_area = area_at(early), area_at(late)
        assert 0 < early_area < late_area, "the later snapshot must be the larger one"
        # An instant between the two resolves to the earlier geometry, not the later.
        assert area_at(dt.datetime(2024, 3, 1, tzinfo=UTC)) == early_area
        # Before any snapshot exists, nothing is served.
        assert area_at(dt.datetime(2023, 1, 1, tzinfo=UTC)) == 0.0

    def test_rebuilding_the_same_instant_replaces_rather_than_duplicates(self):
        instant = dt.datetime(2024, 6, 1, tzinfo=UTC)
        store_upstream("deepstate", [SMALL], "v1", {}, valid_at=instant)
        build(force=True, at=instant)
        build(force=True, at=instant)
        count = db.session.execute(
            text("SELECT count(*) FROM frontline_snapshots WHERE layer='ru' AND valid_at=:v"),
            {"v": instant},
        ).scalar()
        assert count == 1

    def test_history_does_not_inherit_todays_pending_claims(self):
        """A 2024 map must not be dusted with grey buffers from claims opened in 2026."""
        instant = dt.datetime(2024, 6, 1, tzinfo=UTC)
        store_upstream("deepstate", [SMALL], "v1", {}, valid_at=instant)
        db.session.execute(
            text(
                "INSERT INTO frontline_claims (geom, direction, status, created_at) VALUES "
                "(ST_SetSRID(ST_MakePoint(36.0, 48.2),4326), 'ru_advance', 'pending', now())"
            )
        )
        db.session.commit()

        historical = build(force=True, at=instant)
        assert historical["meta"]["pending_claim_ids"] == []

        live = build(force=True)
        assert live["meta"]["pending_claim_ids"], "the live build still applies today's claims"


class TestFrontlineEndpoint:
    def test_at_parameter_returns_the_snapshot_of_that_time(self, client):
        early = dt.datetime(2024, 1, 1, tzinfo=UTC)
        late = dt.datetime(2024, 6, 1, tzinfo=UTC)
        store_upstream("deepstate", [SMALL], "v1", {}, valid_at=early)
        build(force=True, at=early)
        store_upstream("deepstate", [BIG], "v2", {}, valid_at=late)
        build(force=True, at=late)

        payload = client.get(f"/api/frontline?at={quote(early.isoformat())}").get_json()
        assert payload["valid_at"].startswith("2024-01-01")
        assert payload["requested_at"].startswith("2024-01-01")

        newest = client.get("/api/frontline").get_json()
        assert newest["valid_at"].startswith("2024-06-01")

    def test_at_before_all_history_is_empty_not_an_error(self, client):
        store_upstream("deepstate", [SMALL], "v1", {}, valid_at=dt.datetime(2024, 6, 1, tzinfo=UTC))
        build(force=True, at=dt.datetime(2024, 6, 1, tzinfo=UTC))
        resp = client.get(f"/api/frontline?at={quote('2020-01-01T00:00:00+00:00')}")
        assert resp.status_code == 200
        assert resp.get_json()["layers"]["ru"]["coordinates"] == []

    def test_malformed_at_is_a_400(self, client):
        assert client.get("/api/frontline?at=yesterday").status_code == 400

    def test_historical_responses_are_etagged(self, client):
        instant = dt.datetime(2024, 6, 1, tzinfo=UTC)
        store_upstream("deepstate", [SMALL], "v1", {}, valid_at=instant)
        build(force=True, at=instant)
        url = f"/api/frontline?at={quote(instant.isoformat())}"
        first = client.get(url)
        assert first.status_code == 200
        assert client.get(url, headers={"If-None-Match": first.headers["ETag"]}).status_code == 304


class TestTimeline:
    def test_reports_bounds_and_snapshot_instants(self, client, make_source, make_event):
        early = dt.datetime(2024, 1, 1, tzinfo=UTC)
        late = dt.datetime(2024, 6, 1, tzinfo=UTC)
        store_upstream("deepstate", [SMALL], "v1", {}, valid_at=early)
        build(force=True, at=early)
        store_upstream("deepstate", [BIG], "v2", {}, valid_at=late)
        build(force=True, at=late)
        make_event(make_source("russian"), lat=48.2, lon=37.2,
                   occurred_at=dt.datetime(2024, 3, 15, tzinfo=UTC))

        payload = client.get("/api/timeline").get_json()
        assert payload["snapshot_count"] == 2
        assert payload["frontline_from"].startswith("2024-01-01")
        assert payload["frontline_to"].startswith("2024-06-01")
        assert payload["from"].startswith("2024-01-01")
        assert any(s.startswith("2024-06-01") for s in payload["snapshots"])

    def test_empty_database_gives_null_bounds(self, client):
        payload = client.get("/api/timeline").get_json()
        assert payload["from"] is None and payload["to"] is None
        assert payload["snapshot_count"] == 0
        assert payload["snapshots"] == []


class TestRetention:
    """The pruner must thin live duplicates without eating backfilled history."""

    def _snapshot(self, valid_at: dt.datetime, built_at: dt.datetime) -> int:
        row = db.session.execute(
            text(
                "INSERT INTO frontline_snapshots (layer, geom, simplified, generation_meta, "
                "valid_at, built_at) VALUES ('ru', "
                "ST_GeomFromText('MULTIPOLYGON(((37 48,38 48,38 49,37 49,37 48)))', 4326), "
                "'{}'::jsonb, '{}'::jsonb, :valid_at, :built_at) RETURNING id"
            ),
            {"valid_at": valid_at, "built_at": built_at},
        ).first()
        db.session.commit()
        return row.id

    def test_backfilled_history_survives_pruning(self):
        from celery_worker.tasks.maintenance import maintenance

        now = dt.datetime.now(UTC)
        # A backfill: one snapshot per historical day, all built in the same (old) run.
        built_long_ago = now - dt.timedelta(days=200)
        historical = [
            self._snapshot(now - dt.timedelta(days=day), built_long_ago)
            for day in range(150, 160)
        ]
        maintenance()
        survivors = db.session.execute(
            text("SELECT count(*) FROM frontline_snapshots WHERE id = ANY(:ids)"),
            {"ids": historical},
        ).scalar()
        assert survivors == len(historical), "one snapshot per historical day must be kept"

    def test_intraday_duplicates_are_still_collapsed(self):
        from celery_worker.tasks.maintenance import maintenance

        now = dt.datetime.now(UTC)
        day = now - dt.timedelta(days=150)
        # The live builder writing repeatedly within one day.
        ids = [self._snapshot(day + dt.timedelta(minutes=5 * i), now) for i in range(4)]
        maintenance()
        remaining = db.session.execute(
            text("SELECT count(*) FROM frontline_snapshots WHERE id = ANY(:ids)"),
            {"ids": ids},
        ).scalar()
        assert remaining == 1, "same-day snapshots collapse to one"

    def test_recent_snapshots_are_untouched(self):
        from celery_worker.tasks.maintenance import maintenance

        now = dt.datetime.now(UTC)
        ids = [self._snapshot(now - dt.timedelta(minutes=10 * i), now) for i in range(3)]
        maintenance()
        remaining = db.session.execute(
            text("SELECT count(*) FROM frontline_snapshots WHERE id = ANY(:ids)"),
            {"ids": ids},
        ).scalar()
        assert remaining == 3, "inside the retention window nothing is pruned"
