"""Admin auth, CRUD, audit trail and blackout drawing (PLAN §17)."""
from __future__ import annotations

import orjson
from sqlalchemy import text

from app.extensions import db
from app.models import AdminUser, BlackoutZone, Notification, Source

PASSWORD = "test-password-123"


def make_admin(username="admin"):
    user = AdminUser(username=username, password_hash=AdminUser.hash_password(PASSWORD))
    db.session.add(user)
    db.session.commit()
    return user


def login(client, username="admin", password=PASSWORD):
    return client.post(
        "/admin/login", data={"username": username, "password": password}, follow_redirects=False
    )


def audit_actions() -> list[str]:
    return db.session.execute(text("SELECT action FROM audit_log ORDER BY id")).scalars().all()


class TestAuth:
    def test_password_hashing_roundtrip(self):
        user = make_admin()
        assert user.check_password(PASSWORD)
        assert not user.check_password("wrong")
        assert PASSWORD not in user.password_hash

    def test_corrupt_hash_does_not_raise(self):
        user = AdminUser(username="x", password_hash="not-a-bcrypt-hash")
        assert user.check_password(PASSWORD) is False

    def test_login_succeeds_and_is_audited(self, client):
        make_admin()
        resp = login(client)
        assert resp.status_code == 302
        assert "auth.login" in audit_actions()

    def test_bad_password_is_rejected_and_audited(self, client):
        make_admin()
        resp = login(client, password="nope")
        assert resp.status_code == 200          # form redisplayed
        assert "auth.login_failed" in audit_actions()

    def test_protected_pages_redirect_anonymous_users(self, client):
        for path in ("/admin/", "/admin/sources", "/admin/claims", "/admin/blackouts"):
            resp = client.get(path)
            assert resp.status_code == 302
            assert "/admin/login" in resp.headers["Location"]

    def test_rate_limit_kicks_in_after_five_attempts(self, client):
        make_admin()
        codes = [login(client, password="wrong").status_code for _ in range(7)]
        assert 429 in codes, "login must be rate limited per IP"

    def test_logout(self, client):
        make_admin()
        login(client)
        assert client.post("/admin/logout").status_code == 302
        assert client.get("/admin/").status_code == 302


class TestDashboard:
    def test_dashboard_renders_with_health_and_sources(self, client, make_source):
        make_admin()
        login(client)
        make_source("ukrainian", name="Ukrinform")
        body = client.get("/admin/").get_data(as_text=True)
        assert "Dashboard" in body
        assert "Ukrinform" in body
        assert "Postgres" in body


class TestSources:
    def test_create_edit_and_audit(self, client):
        make_admin()
        login(client)
        resp = client.post(
            "/admin/sources/new",
            data={
                "name": "New Feed", "type": "rss", "url": "https://feed.test/rss",
                "perspective": "western", "reliability_tier": "2", "poll_interval_s": "900",
                "enabled": "y", "meta_json": '{"note":"hi"}',
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        source = db.session.execute(db.select(Source).where(Source.name == "New Feed")).scalar_one()
        assert source.enabled and source.meta == {"note": "hi"}
        assert "source.save" in audit_actions()

        client.post(
            f"/admin/sources/{source.id}",
            data={
                "name": "Renamed Feed", "type": "rss", "url": "https://feed.test/rss",
                "perspective": "neutral", "reliability_tier": "1", "poll_interval_s": "600",
                "meta_json": "{}",
            },
        )
        db.session.expire_all()
        source = db.session.get(Source, source.id)
        assert source.name == "Renamed Feed"
        assert source.perspective == "neutral"
        assert source.enabled is False       # unchecked box clears it

    def test_invalid_meta_json_is_rejected(self, client):
        make_admin()
        login(client)
        resp = client.post(
            "/admin/sources/new",
            data={
                "name": "Bad Meta", "type": "rss", "url": "https://feed.test/rss",
                "perspective": "western", "reliability_tier": "2", "poll_interval_s": "900",
                "meta_json": "{not json",
            },
        )
        assert resp.status_code == 200
        assert db.session.execute(db.select(Source).where(Source.name == "Bad Meta")).first() is None

    def test_reset_failures(self, client, make_source):
        make_admin()
        login(client)
        source = make_source("western")
        source.consecutive_failures = 9
        source.status = "degraded"
        db.session.commit()
        client.post(f"/admin/sources/{source.id}/reset")
        db.session.expire_all()
        fresh = db.session.get(Source, source.id)
        assert fresh.consecutive_failures == 0 and fresh.status == "ok"
        assert "source.reset_failures" in audit_actions()

    def test_delete_with_history_disables_instead_of_deleting(self, client, make_source, make_event):
        make_admin()
        login(client)
        source = make_source("western")
        make_event(source, lat=48.5, lon=37.5)
        client.post(f"/admin/sources/{source.id}/delete")
        db.session.expire_all()
        fresh = db.session.get(Source, source.id)
        assert fresh is not None, "sources with stored news must not be orphaned"
        assert fresh.enabled is False and fresh.status == "dead"

    def test_delete_without_history_removes_the_row(self, client, make_source):
        make_admin()
        login(client)
        source = make_source("western")
        source_id = source.id
        client.post(f"/admin/sources/{source_id}/delete")
        db.session.expire_all()
        assert db.session.get(Source, source_id) is None

    def test_test_fetch_reports_adapter_errors_without_500ing(self, client, make_source, monkeypatch):
        make_admin()
        login(client)
        source = make_source("western", type_="rss")

        def boom(_source):
            raise RuntimeError("upstream exploded")

        monkeypatch.setattr("celery_worker.adapters.fetch_source_preview", boom)
        resp = client.post(f"/admin/sources/{source.id}/test")
        assert resp.status_code == 200
        assert "upstream exploded" in resp.get_data(as_text=True)


class TestNewsAndEvents:
    def test_hide_and_unhide_news(self, client, make_source, make_event):
        make_admin()
        login(client)
        event = make_event(make_source("western"), lat=48.5, lon=37.5)
        item_id = event.news_item_id

        client.post(f"/admin/news/{item_id}/visible")
        db.session.expire_all()
        assert db.session.execute(
            text("SELECT visible FROM news_items WHERE id=:i"), {"i": item_id}
        ).scalar() is False

        client.post(f"/admin/news/{item_id}/visible")
        db.session.expire_all()
        assert db.session.execute(
            text("SELECT visible FROM news_items WHERE id=:i"), {"i": item_id}
        ).scalar() is True
        assert audit_actions().count("news.visibility") == 2

    def test_relocate_event(self, client, make_source, make_event):
        make_admin()
        login(client)
        event = make_event(make_source("western"), lat=48.5, lon=37.5)
        client.post(f"/admin/events/{event.id}/location", data={"lat": "49.1", "lon": "36.2"})
        db.session.expire_all()
        row = db.session.execute(
            text("SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon, coord_source "
                 "FROM extracted_events WHERE id=:i"),
            {"i": event.id},
        ).first()
        assert round(row.lat, 3) == 49.1 and round(row.lon, 3) == 36.2
        assert row.coord_source == "gazetteer_match"
        assert "event.relocate" in audit_actions()

    def test_relocate_rejects_non_numeric_input(self, client, make_source, make_event):
        make_admin()
        login(client)
        event = make_event(make_source("western"), lat=48.5, lon=37.5)
        client.post(f"/admin/events/{event.id}/location", data={"lat": "north", "lon": "west"})
        db.session.expire_all()
        row = db.session.execute(
            text("SELECT ST_Y(geom) AS lat FROM extracted_events WHERE id=:i"), {"i": event.id}
        ).first()
        assert round(row.lat, 3) == 48.5, "a bad edit must leave the geometry alone"

    def test_delete_news_cascades_to_events(self, client, make_source, make_event):
        make_admin()
        login(client)
        event = make_event(make_source("western"), lat=48.5, lon=37.5)
        client.post(f"/admin/news/{event.news_item_id}/delete")
        db.session.expire_all()
        assert db.session.execute(text("SELECT count(*) FROM extracted_events")).scalar() == 0

    def test_reextract_clears_events_and_requeues(self, client, make_source, make_event, monkeypatch):
        make_admin()
        login(client)
        event = make_event(make_source("western"), lat=48.5, lon=37.5)
        item_id = event.news_item_id
        monkeypatch.setattr(
            "celery_worker.tasks.extract.llm_extract_batch.delay", lambda **kw: None
        )
        client.post(f"/admin/news/{item_id}/reextract")
        db.session.expire_all()
        row = db.session.execute(
            text("SELECT llm_status, llm_attempts FROM news_items WHERE id=:i"), {"i": item_id}
        ).first()
        assert row.llm_status == "pending" and row.llm_attempts == 0
        assert db.session.execute(text("SELECT count(*) FROM extracted_events")).scalar() == 0


class TestClaimsReview:
    def _claim(self):
        row = db.session.execute(
            text(
                "INSERT INTO frontline_claims (geom, direction, status) VALUES "
                "(ST_SetSRID(ST_MakePoint(37.8,48.6),4326), 'ru_advance', 'pending') RETURNING id"
            )
        ).first()
        db.session.commit()
        return row.id

    def test_manual_confirm_sets_admin_attribution_and_dirty_flag(self, client, monkeypatch):
        make_admin()
        login(client)
        claim_id = self._claim()
        monkeypatch.setattr("celery_worker.tasks.frontline.rebuild_frontline.delay", lambda: None)

        client.post(f"/admin/claims/{claim_id}/confirm")
        db.session.expire_all()
        row = db.session.execute(
            text("SELECT status, resolved_by FROM frontline_claims WHERE id=:i"), {"i": claim_id}
        ).first()
        assert row.status == "confirmed"
        assert row.resolved_by == "admin:admin"
        from app.services.cache import frontline_is_dirty

        assert frontline_is_dirty()
        assert "claim.confirmed" in audit_actions()

    def test_unknown_action_is_a_400(self, client):
        make_admin()
        login(client)
        claim_id = self._claim()
        assert client.post(f"/admin/claims/{claim_id}/frobnicate").status_code == 400

    def test_claims_page_shows_the_evidence_chain(self, client, make_source, make_event, monkeypatch):
        make_admin()
        login(client)
        source = make_source("russian", name="Rybar")
        event = make_event(source, lat=48.6, lon=37.8)
        claim_id = self._claim()
        db.session.execute(
            text(
                "INSERT INTO evidence_links (claim_id, event_id, role, active) "
                "VALUES (:c, :e, 'support', true)"
            ),
            {"c": claim_id, "e": event.id},
        )
        db.session.commit()
        body = client.get("/admin/claims?status=pending").get_data(as_text=True)
        assert "Rybar" in body
        assert "russian" in body
        assert "support" in body

    def test_evidence_can_be_deactivated(self, client, make_source, make_event, monkeypatch):
        make_admin()
        login(client)
        event = make_event(make_source("russian"), lat=48.6, lon=37.8)
        claim_id = self._claim()
        link_id = db.session.execute(
            text(
                "INSERT INTO evidence_links (claim_id, event_id, role, active) "
                "VALUES (:c, :e, 'support', true) RETURNING id"
            ),
            {"c": claim_id, "e": event.id},
        ).first().id
        db.session.commit()
        monkeypatch.setattr("celery_worker.tasks.rules.evaluate_claims.delay", lambda: None)

        client.post(f"/admin/evidence/{link_id}/deactivate")
        db.session.expire_all()
        assert db.session.execute(
            text("SELECT active FROM evidence_links WHERE id=:i"), {"i": link_id}
        ).scalar() is False
        assert "evidence.deactivate" in audit_actions()


class TestNotifications:
    def test_crud(self, client):
        make_admin()
        login(client)
        client.post(
            "/admin/notifications/new",
            data={"title": "Heads up", "body": "maintenance", "level": "warning", "active": "y"},
        )
        row = db.session.execute(db.select(Notification)).scalar_one()
        assert row.title == "Heads up" and row.level == "warning" and row.active

        client.post(f"/admin/notifications/{row.id}", data={"title": "Edited", "level": "info"})
        db.session.expire_all()
        row = db.session.get(Notification, row.id)
        assert row.title == "Edited" and row.active is False

        client.post(f"/admin/notifications/{row.id}/delete")
        db.session.expire_all()
        assert db.session.execute(db.select(Notification)).first() is None
        assert "notification.delete" in audit_actions()


class TestBlackouts:
    POLYGON = {
        "type": "Polygon",
        "coordinates": [[[37.0, 48.0], [38.0, 48.0], [38.0, 49.0], [37.0, 49.0], [37.0, 48.0]]],
    }

    def test_create_toggle_delete(self, client):
        make_admin()
        login(client)
        client.post(
            "/admin/blackouts",
            data={"name": "Zone A", "reason": "opsec", "geojson": orjson.dumps(self.POLYGON).decode(),
                  "active": "y"},
        )
        zone = db.session.execute(db.select(BlackoutZone)).scalar_one()
        assert zone.name == "Zone A" and zone.active

        client.post(f"/admin/blackouts/{zone.id}/toggle")
        db.session.expire_all()
        assert db.session.get(BlackoutZone, zone.id).active is False

        client.post(f"/admin/blackouts/{zone.id}/delete")
        db.session.expire_all()
        assert db.session.execute(db.select(BlackoutZone)).first() is None
        assert "blackout.create" in audit_actions()

    def test_a_feature_wrapper_is_unwrapped(self, client):
        make_admin()
        login(client)
        feature = {"type": "Feature", "properties": {}, "geometry": self.POLYGON}
        client.post(
            "/admin/blackouts",
            data={"name": "Wrapped", "geojson": orjson.dumps(feature).decode(), "active": "y"},
        )
        assert db.session.execute(db.select(BlackoutZone)).scalar_one().name == "Wrapped"

    def test_non_polygon_geometry_is_rejected(self, client):
        make_admin()
        login(client)
        client.post(
            "/admin/blackouts",
            data={"name": "Point zone",
                  "geojson": orjson.dumps({"type": "Point", "coordinates": [37, 48]}).decode()},
        )
        assert db.session.execute(db.select(BlackoutZone)).first() is None

    def test_malformed_json_is_rejected(self, client):
        make_admin()
        login(client)
        client.post("/admin/blackouts", data={"name": "Broken", "geojson": "{oops"})
        assert db.session.execute(db.select(BlackoutZone)).first() is None
