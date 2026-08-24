"""Frontline builder: grey-zone symmetric difference, claim application, revert idempotence (§14, §20)."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from app.extensions import db
from app.services.frontline import build, latest_snapshots, store_upstream

# Two overlapping squares: the intersection is agreed control, the symmetric difference is grey.
DS_SQUARE = {"type": "Polygon", "coordinates": [[[37.0, 48.0], [38.0, 48.0], [38.0, 49.0], [37.0, 49.0], [37.0, 48.0]]]}
ISW_SQUARE = {"type": "Polygon", "coordinates": [[[37.5, 48.0], [38.5, 48.0], [38.5, 49.0], [37.5, 49.0], [37.5, 48.0]]]}
SMALL_SQUARE = {"type": "Polygon", "coordinates": [[[37.0, 48.0], [37.4, 48.0], [37.4, 48.4], [37.0, 48.4], [37.0, 48.0]]]}


def area_of(layer: str) -> float:
    return float(
        db.session.execute(
            text(
                "SELECT ST_Area(geom::geography)/1e6 FROM frontline_snapshots "
                "WHERE layer = :layer ORDER BY built_at DESC LIMIT 1"
            ),
            {"layer": layer},
        ).scalar()
        or 0.0
    )


def seed_upstreams():
    store_upstream("deepstate", [DS_SQUARE], "ds-1", {"provider": "deepstate"})
    store_upstream("isw", [ISW_SQUARE], "isw-1", {"provider": "isw"})


class TestBaselineBuild:
    def test_ru_is_the_intersection_and_grey_is_the_symmetric_difference(self):
        seed_upstreams()
        result = build(force=True)
        assert result["status"] == "ok"
        assert set(result["layers"]) == {"ru", "grey"}

        ru_area, grey_area = area_of("ru"), area_of("grey")
        # The overlap is half of each square; the symmetric difference is the other two halves.
        assert ru_area > 0 and grey_area > 0
        assert grey_area == max(grey_area, ru_area * 1.5), "grey (2 halves) should exceed ru (1 half)"

    def test_ru_and_grey_do_not_overlap(self):
        seed_upstreams()
        build(force=True)
        overlap = db.session.execute(
            text(
                """
                SELECT ST_Area(ST_Intersection(ru.geom, grey.geom)::geography) AS a
                FROM (SELECT geom FROM frontline_snapshots WHERE layer='ru'
                      ORDER BY built_at DESC LIMIT 1) ru,
                     (SELECT geom FROM frontline_snapshots WHERE layer='grey'
                      ORDER BY built_at DESC LIMIT 1) grey
                """
            )
        ).scalar()
        assert float(overlap or 0) < 1.0  # square metres, i.e. only boundary slivers

    def test_single_upstream_is_recorded_as_degraded(self):
        store_upstream("deepstate", [DS_SQUARE], "ds-1", {"provider": "deepstate"})
        result = build(force=True)
        assert result["status"] == "ok"
        assert result["meta"]["degraded"] == "single_upstream:deepstate"
        assert area_of("ru") > 0
        assert area_of("grey") == 0  # no second view to disagree with

    def test_no_upstream_at_all_is_a_no_op(self):
        assert build(force=True)["status"] == "no_upstream"

    def test_three_detail_tiers_are_stored(self):
        seed_upstreams()
        build(force=True)
        payload = latest_snapshots("high")
        assert payload["detail"] == "high"
        for tier in ("low", "mid", "high"):
            assert latest_snapshots(tier)["layers"]["ru"]["type"] in ("MultiPolygon", "Polygon")

    def test_unknown_detail_tier_falls_back_to_mid(self):
        seed_upstreams()
        build(force=True)
        assert latest_snapshots("nonsense")["detail"] == "mid"

    def test_serves_last_snapshot_when_upstreams_are_stale(self):
        seed_upstreams()
        build(force=True)
        before = latest_snapshots("mid")
        # Age both upstreams well past the staleness horizon.
        db.session.execute(text("UPDATE upstream_geometries SET fetched_at = now() - interval '30 days'"))
        db.session.commit()
        build(force=True)
        after = latest_snapshots("mid")
        assert after["layers"]["ru"] == before["layers"]["ru"], "stale upstreams still serve geometry"


class TestClaimApplication:
    def _confirm_claim(self, direction: str, lon: float, lat: float, when=None):
        when = when or dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
        row = db.session.execute(
            text(
                "INSERT INTO frontline_claims (geom, direction, status, resolved_at, resolved_by) "
                "VALUES (ST_SetSRID(ST_MakePoint(:lon,:lat),4326), :direction, 'confirmed', "
                ":when, 'rule:corroboration') RETURNING id"
            ),
            {"lon": lon, "lat": lat, "direction": direction, "when": when},
        ).first()
        db.session.commit()
        return row.id

    def test_pending_claim_adds_a_grey_buffer(self):
        seed_upstreams()
        build(force=True)
        grey_before = area_of("grey")

        db.session.execute(
            text(
                "INSERT INTO frontline_claims (geom, direction, status) VALUES "
                "(ST_SetSRID(ST_MakePoint(36.5, 48.5),4326), 'ru_advance', 'pending')"
            )
        )
        db.session.commit()
        build(force=True)
        assert area_of("grey") > grey_before, "GREY_BUFFER_KM around a pending claim must show up"

    def test_confirmed_ru_advance_grows_ru_control(self):
        seed_upstreams()
        build(force=True)
        ru_before = area_of("ru")
        self._confirm_claim("ru_advance", 36.5, 48.5)   # west of both baselines
        build(force=True)
        assert area_of("ru") > ru_before

    def test_confirmed_ua_advance_shrinks_ru_control(self):
        seed_upstreams()
        build(force=True)
        ru_before = area_of("ru")
        self._confirm_claim("ua_advance", 37.75, 48.5)  # inside the agreed overlap
        build(force=True)
        assert area_of("ru") < ru_before

    def test_claims_older_than_the_baselines_are_not_reapplied(self):
        seed_upstreams()
        build(force=True)
        ru_before = area_of("ru")
        # Resolved before the baselines were fetched → already absorbed upstream.
        self._confirm_claim(
            "ru_advance", 36.5, 48.5, when=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)
        )
        build(force=True)
        assert abs(area_of("ru") - ru_before) < 0.01

    def test_revert_removes_the_geometry_contribution(self):
        seed_upstreams()
        build(force=True)
        baseline = area_of("ru")

        claim_id = self._confirm_claim("ru_advance", 36.5, 48.5)
        build(force=True)
        grown = area_of("ru")
        assert grown > baseline

        db.session.execute(
            text("UPDATE frontline_claims SET status = 'reverted' WHERE id = :cid"), {"cid": claim_id}
        )
        db.session.commit()
        build(force=True)
        assert abs(area_of("ru") - baseline) < 0.01, "a reverted claim must leave no trace"

    def test_rebuild_is_idempotent(self):
        seed_upstreams()
        self._confirm_claim("ru_advance", 36.5, 48.5)
        build(force=True)
        first_ru, first_grey = area_of("ru"), area_of("grey")
        build(force=True)
        assert abs(area_of("ru") - first_ru) < 1e-6
        assert abs(area_of("grey") - first_grey) < 1e-6

    def test_generation_meta_records_inputs(self):
        seed_upstreams()
        claim_id = self._confirm_claim("ru_advance", 36.5, 48.5)
        result = build(force=True)
        meta = result["meta"]
        assert meta["deepstate_id"] == "ds-1"
        assert meta["isw_editdate"] == "isw-1"
        assert claim_id in meta["applied_claim_ids"]


class TestUpstreamStorage:
    def test_z_ordinates_and_invalid_rings_are_tolerated(self):
        # A bowtie polygon (self-intersecting) must be repaired, not rejected.
        bowtie = {
            "type": "Polygon",
            "coordinates": [[[37.0, 48.0], [38.0, 49.0], [38.0, 48.0], [37.0, 49.0], [37.0, 48.0]]],
        }
        row = store_upstream("deepstate", [bowtie], "ds-bowtie", {})
        valid = db.session.execute(
            text("SELECT ST_IsValid(geom) FROM upstream_geometries WHERE id = :i"), {"i": row.id}
        ).scalar()
        assert valid is True

    def test_empty_input_raises(self):
        import pytest

        with pytest.raises(ValueError):
            store_upstream("deepstate", [], "none", {})


class TestGreyClaimExpiry:
    """A sighting that is never repeated stops painting grey (PLAN §14.2 + GREY_CLAIM_TTL_DAYS)."""

    def _claim_with_evidence(self, *, sighted_days_ago: float, lon=36.5, lat=48.5,
                             active=True) -> int:
        """A pending claim whose newest active evidence was reported N days ago."""
        import datetime as dt

        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=sighted_days_ago)
        claim_id = db.session.execute(
            text(
                "INSERT INTO frontline_claims (geom, direction, status, created_at) VALUES "
                "(ST_SetSRID(ST_MakePoint(:lon,:lat),4326), 'ru_advance', 'pending', :when) "
                "RETURNING id"
            ),
            {"lon": lon, "lat": lat, "when": when},
        ).first().id
        source_id = db.session.execute(
            text(
                "INSERT INTO sources (name, type, url, perspective) VALUES "
                "('t', 'rss', :url, 'neutral') RETURNING id"
            ),
            {"url": f"https://example.test/{claim_id}"},
        ).first().id
        item_id = db.session.execute(
            text(
                "INSERT INTO news_items (source_id, content_hash, title, llm_status, published_at) "
                "VALUES (:sid, :h, 'sighting', 'done', :when) RETURNING id"
            ),
            {"sid": source_id, "h": f"hash-{claim_id}", "when": when},
        ).first().id
        event_id = db.session.execute(
            text(
                "INSERT INTO extracted_events (news_item_id, event_type, occurred_at, "
                "coord_source, geom) VALUES (:iid, 'geolocation_proof', :when, 'explicit_coords', "
                "ST_SetSRID(ST_MakePoint(:lon,:lat),4326)) RETURNING id"
            ),
            {"iid": item_id, "when": when, "lon": lon, "lat": lat},
        ).first().id
        db.session.execute(
            text(
                "INSERT INTO evidence_links (claim_id, event_id, role, active) "
                "VALUES (:c, :e, 'geolocation_proof', :active)"
            ),
            {"c": claim_id, "e": event_id, "active": active},
        )
        db.session.commit()
        return claim_id

    def test_a_fresh_sighting_paints_grey(self):
        seed_upstreams()
        build(force=True)
        before = area_of("grey")
        self._claim_with_evidence(sighted_days_ago=1)
        build(force=True)
        assert area_of("grey") > before

    def test_a_sighting_not_repeated_within_a_week_stops_painting_grey(self):
        seed_upstreams()
        build(force=True)
        baseline = area_of("grey")
        claim_id = self._claim_with_evidence(sighted_days_ago=10)
        result = build(force=True)
        assert claim_id not in result["meta"]["pending_claim_ids"]
        assert abs(area_of("grey") - baseline) < 0.01, "a stale sighting adds no halo"

    def test_the_claim_itself_survives_for_audit(self):
        """Only the halo expires — the claim stays pending and can still be confirmed."""
        seed_upstreams()
        claim_id = self._claim_with_evidence(sighted_days_ago=10)
        build(force=True)
        status = db.session.execute(
            text("SELECT status FROM frontline_claims WHERE id = :i"), {"i": claim_id}
        ).scalar()
        assert status == "pending"

    def test_a_repeated_sighting_keeps_the_area_grey(self):
        """The old report alone would have expired; a fresh one renews the claim's halo."""
        import datetime as dt

        seed_upstreams()
        build(force=True)
        baseline = area_of("grey")
        claim_id = self._claim_with_evidence(sighted_days_ago=10)

        # A second, recent sighting of the same spot.
        recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        item_id = db.session.execute(
            text(
                "INSERT INTO news_items (source_id, content_hash, title, llm_status, published_at) "
                "SELECT source_id, 'hash-repeat', 'again', 'done', :when FROM news_items LIMIT 1 "
                "RETURNING id"
            ),
            {"when": recent},
        ).first().id
        event_id = db.session.execute(
            text(
                "INSERT INTO extracted_events (news_item_id, event_type, occurred_at, "
                "coord_source, geom) VALUES (:iid, 'geolocation_proof', :when, 'explicit_coords', "
                "ST_SetSRID(ST_MakePoint(36.5,48.5),4326)) RETURNING id"
            ),
            {"iid": item_id, "when": recent},
        ).first().id
        db.session.execute(
            text(
                "INSERT INTO evidence_links (claim_id, event_id, role, active) "
                "VALUES (:c, :e, 'geolocation_proof', true)"
            ),
            {"c": claim_id, "e": event_id},
        )
        db.session.commit()

        result = build(force=True)
        assert claim_id in result["meta"]["pending_claim_ids"]
        assert area_of("grey") > baseline

    def test_debunked_evidence_stops_counting_as_a_repeat(self):
        """A retracted sighting must not keep an area grey."""
        seed_upstreams()
        build(force=True)
        baseline = area_of("grey")
        # Reported recently, but the link has been deactivated by a debunk.
        claim_id = self._claim_with_evidence(sighted_days_ago=1, active=False)
        # The claim was created "1 day ago" too, so it is the evidence — not the age — being tested.
        db.session.execute(
            text("UPDATE frontline_claims SET created_at = now() - interval '30 days' "
                 "WHERE id = :i"),
            {"i": claim_id},
        )
        db.session.commit()
        result = build(force=True)
        assert claim_id not in result["meta"]["pending_claim_ids"]
        assert abs(area_of("grey") - baseline) < 0.01

    def test_historical_builds_ask_whether_it_was_fresh_then(self):
        """Scrubbing back must show the halo as it stood at that moment, not today's verdict."""
        import datetime as dt

        # Upstream geometry has to predate the instant being rebuilt, or there is nothing to build.
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        store_upstream("deepstate", [DS_SQUARE], "ds-old", {}, valid_at=old)
        store_upstream("isw", [ISW_SQUARE], "isw-old", {}, valid_at=old)
        claim_id = self._claim_with_evidence(sighted_days_ago=10)
        # Two days after the sighting it was still fresh.
        soon_after = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
        result = build(force=True, at=soon_after)
        assert claim_id in result["meta"]["pending_claim_ids"]

        # Today, it is not.
        assert claim_id not in build(force=True)["meta"]["pending_claim_ids"]


class TestGreyBufferSize:
    def test_the_halo_matches_the_configured_radius(self):
        """The buffer is a real radius in km, not an arbitrary constant."""
        import math

        from app.config import settings

        seed_upstreams()
        build(force=True)
        baseline = area_of("grey")
        # Far from the baselines so its halo is the only thing added.
        db.session.execute(
            text(
                "INSERT INTO frontline_claims (geom, direction, status) VALUES "
                "(ST_SetSRID(ST_MakePoint(34.0, 48.5),4326), 'ru_advance', 'pending')"
            )
        )
        db.session.commit()
        build(force=True)
        added = area_of("grey") - baseline
        expected = math.pi * settings.grey_buffer_km ** 2
        assert added == pytest.approx(expected, rel=0.05), (
            f"a {settings.grey_buffer_km} km halo should add ~{expected:.1f} km², got {added:.1f}"
        )


class TestBaselineCutoff:
    """A confirmed claim is only applied while it is genuinely ahead of both baselines."""

    def _confirm(self, resolved_at) -> int:
        row = db.session.execute(
            text(
                "INSERT INTO frontline_claims (geom, direction, status, resolved_at, resolved_by) "
                "VALUES (ST_SetSRID(ST_MakePoint(34.0, 48.5),4326), 'ru_advance', 'confirmed', "
                ":when, 'rule:corroboration') RETURNING id"
            ),
            {"when": resolved_at},
        ).first()
        db.session.commit()
        return row.id

    def test_claims_newer_than_the_baseline_are_applied(self):
        import datetime as dt

        published = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)
        store_upstream("deepstate", [DS_SQUARE], "ds", {}, valid_at=published)
        claim_id = self._confirm(published + dt.timedelta(hours=1))
        assert claim_id in build(force=True)["meta"]["applied_claim_ids"]

    def test_claims_the_baseline_already_reflects_are_not_reapplied(self):
        import datetime as dt

        published = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)
        store_upstream("deepstate", [DS_SQUARE], "ds", {}, valid_at=published)
        claim_id = self._confirm(published - dt.timedelta(days=2))
        assert claim_id not in build(force=True)["meta"]["applied_claim_ids"]

    def test_the_cutoff_follows_publication_not_download_time(self):
        """A later backfill write must not change which claims are considered outstanding."""
        import datetime as dt

        published = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)
        store_upstream("deepstate", [DS_SQUARE], "ds", {}, valid_at=published)
        claim_id = self._confirm(published + dt.timedelta(hours=1))
        before = build(force=True)["meta"]["applied_claim_ids"]

        # A historical row written *now* but depicting last year.
        store_upstream("deepstate", [SMALL_SQUARE], "ds-old", {},
                       valid_at=published - dt.timedelta(days=400))
        after = build(force=True)["meta"]["applied_claim_ids"]
        assert before == after, "storing older history must not change the cutoff"
