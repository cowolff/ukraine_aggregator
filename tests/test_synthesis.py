"""Cross-source synthesis (plans/SYNTHESIS.md): clustering, credibility, LLM stage, API."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import text

from app.extensions import db
from app.models import ExtractedEvent, NewsItem, SynthesizedReport, content_hash
from app.services import llm
from app.services.synthesis import cluster_events, compute_credibility


def _utc(hours_ago: float = 0) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)


def _reports() -> list[SynthesizedReport]:
    return db.session.query(SynthesizedReport).order_by(SynthesizedReport.id).all()


def _stored_report(**overrides) -> SynthesizedReport:
    """Insert a report row directly, bypassing clustering — for close/refresh/API tests."""
    fields = {
        "event_type": "shelling",
        "geom": "SRID=4326;POINT(37.0 48.0)",
        "status": "open",
        "member_count": 3,
        "synthesized_member_count": 3,
        "first_reported_at": _utc(2),
        "last_reported_at": _utc(1),
        "credibility": "reported",
        "cred_meta": {"sources": 3, "classes": ["ukrainian"], "best_tier": 2,
                      "tiers": {"2": 3}, "disputed": False},
    }
    fields.update(overrides)
    report = SynthesizedReport(**fields)
    db.session.add(report)
    db.session.commit()
    return report


class TestClustering:
    def test_close_same_type_events_join_one_report(self, make_source, make_event):
        sources = [make_source("ukrainian"), make_source("russian"), make_source("western")]
        for i, source in enumerate(sources):
            make_event(source, event_type="shelling", lat=48.0 + i * 0.005, lon=37.0)
        result = cluster_events()
        assert result.founded == 1 and result.joined == 2
        (report,) = _reports()
        assert report.member_count == 3
        assert report.event_type == "shelling"
        assert report.first_reported_at is not None

    def test_distant_events_found_separate_reports(self, make_source, make_event):
        make_event(make_source("ukrainian"), event_type="shelling", lat=48.0, lon=37.0)
        make_event(make_source("russian"), event_type="shelling", lat=48.1, lon=37.0)  # ~11 km
        result = cluster_events()
        assert result.founded == 2 and result.joined == 0

    def test_types_never_mix(self, make_source, make_event):
        make_event(make_source("ukrainian"), event_type="shelling", lat=48.0, lon=37.0)
        make_event(make_source("russian"), event_type="deep_strike", lat=48.0, lon=37.0)
        result = cluster_events()
        assert result.founded == 2 and result.joined == 0

    def test_arming_needs_distinct_sources_not_items(self, make_source, make_event):
        source = make_source("ukrainian")
        for _ in range(3):
            make_event(source, event_type="shelling", lat=48.0, lon=37.0)
        cluster_events()
        (report,) = _reports()
        assert report.member_count == 3
        assert report.cred_meta["sources"] == 1, "one source posting thrice is one source"
        assert report.llm_status is None, "single-source clusters never arm"

    def test_threshold_arms_the_llm_stage(self, make_source, make_event):
        for perspective in ("ukrainian", "russian", "western"):
            make_event(make_source(perspective), event_type="shelling", lat=48.0, lon=37.0)
        result = cluster_events()
        assert result.armed == 1
        (report,) = _reports()
        assert report.llm_status == "pending"
        assert report.credibility is not None

    def test_digest_items_never_cluster(self, make_source):
        """A roundup naming many settlements must not manufacture corroboration."""
        source = make_source("ukrainian")
        item = NewsItem(
            source_id=source.id, content_hash=content_hash("digest", "digest"),
            title="Overnight digest", body="strikes everywhere", llm_status="done",
        )
        db.session.add(item)
        db.session.flush()
        for i in range(6):  # > claim_max_locations distinct places
            db.session.add(ExtractedEvent(
                news_item_id=item.id, event_type="shelling",
                place_name_raw=f"Town {i}", occurred_at=_utc(0),
            ))
        db.session.commit()
        db.session.execute(text(
            "UPDATE extracted_events "
            "SET geom = ST_SetSRID(ST_MakePoint(37.0 + id * 0.001, 48.0), 4326)"
        ))
        db.session.commit()
        result = cluster_events()
        assert result.digests_skipped == 6
        assert result.founded == 0 and _reports() == []

    def test_debunks_join_but_never_found(self, make_source, make_event):
        make_event(make_source("western"), event_type="debunk", lat=48.0, lon=37.0)
        result = cluster_events()
        assert result.events_seen == 1 and result.founded == 0
        assert _reports() == [], "a debunk with nothing to debunk founds nothing"

        for perspective in ("ukrainian", "russian"):
            make_event(make_source(perspective), event_type="shelling", lat=48.0, lon=37.0)
        result = cluster_events()
        assert result.founded == 1
        (report,) = _reports()
        # The orphan debunk was never made a member, is still in the window, and joins the new
        # report on this same pass.
        assert report.member_count == 3
        assert report.credibility == "unverified", "an active debunk caps the verdict"
        assert report.cred_meta["disputed"] is True

    def test_low_confidence_events_are_not_candidates(self, make_source, make_event):
        make_event(make_source("ukrainian"), event_type="shelling", lat=48.0, lon=37.0,
                   confidence=0.2)
        result = cluster_events()
        assert result.events_seen == 0 and _reports() == []

    def test_expired_reports_close_and_get_a_final_pass(self):
        _stored_report(status="open", llm_status="done",
                       member_count=5, synthesized_member_count=3,
                       first_reported_at=_utc(40), last_reported_at=_utc(30))
        result = cluster_events()
        assert result.closed == 1 and result.refreshed == 1
        (report,) = _reports()
        assert report.status == "closed"
        assert report.llm_status == "pending", "unsynthesized members get one final pass"

    def test_closed_unarmed_reports_are_pruned(self):
        _stored_report(status="closed", llm_status=None,
                       first_reported_at=_utc(24 * 9), last_reported_at=_utc(24 * 8))
        result = cluster_events()
        assert result.pruned == 1 and _reports() == []

    def test_refresh_hysteresis(self, make_source, make_event):
        for perspective in ("ukrainian", "russian", "western"):
            make_event(make_source(perspective), event_type="shelling", lat=48.0, lon=37.0)
        cluster_events()
        (report,) = _reports()
        report.llm_status = "done"
        report.synthesized_member_count = report.member_count
        db.session.commit()

        make_event(make_source("ukrainian"), event_type="shelling", lat=48.0, lon=37.0)
        cluster_events()
        db.session.expire_all()
        (report,) = _reports()
        assert report.llm_status == "done", "one repost must not burn a prompt"

        for _ in range(2):
            make_event(make_source("russian"), event_type="shelling", lat=48.0, lon=37.0)
        result = cluster_events()
        db.session.expire_all()
        (report,) = _reports()
        assert report.llm_status == "pending" and result.refreshed == 1


class TestCredibility:
    _next_id = iter(range(1, 1000))

    def _row(self, perspective="ukrainian", tier=2, *, geoproof=False, debunk=False,
             reports_it=True):
        return {
            "source_id": next(self._next_id),
            "perspective": perspective,
            "reliability_tier": tier,
            "has_geoproof": geoproof,
            "has_debunk": debunk,
            "reports_it": reports_it,
        }

    def test_two_solid_perspective_classes_confirm(self):
        verdict, meta = compute_credibility(
            [self._row("ukrainian", 2), self._row("russian", 2), self._row("ukrainian", 3)]
        )
        assert verdict == "confirmed"
        assert meta["classes"] == ["russian", "ukrainian"]
        assert meta["tiers"] == {"2": 2, "3": 1}

    def test_tier1_geolocation_confirms_alone(self):
        verdict, _ = compute_credibility(
            [self._row("western", 1, geoproof=True), self._row("western", 3)]
        )
        assert verdict == "confirmed"

    def test_two_weak_classes_corroborate(self):
        verdict, _ = compute_credibility([self._row("ukrainian", 3), self._row("russian", 3)])
        assert verdict == "corroborated"

    def test_neutral_counts_as_western(self):
        """neutral+western is one class of view, not two (same reading as the rules engine)."""
        verdict, meta = compute_credibility([self._row("neutral", 2), self._row("western", 2)])
        assert meta["classes"] == ["western"]
        assert verdict == "reported"

    def test_single_class_with_tier1_and_three_sources_corroborates(self):
        verdict, _ = compute_credibility(
            [self._row("ukrainian", 1), self._row("ukrainian", 2), self._row("ukrainian", 3)]
        )
        assert verdict == "corroborated"

    def test_single_class_solid_tier_is_reported(self):
        verdict, _ = compute_credibility([self._row("ukrainian", 2), self._row("ukrainian", 3)])
        assert verdict == "reported"

    def test_tier3_only_is_unverified(self):
        verdict, meta = compute_credibility([self._row("russian", 3), self._row("russian", 3)])
        assert verdict == "unverified"
        assert meta["best_tier"] == 3

    def test_a_debunk_caps_any_verdict(self):
        rows = [self._row("ukrainian", 1), self._row("russian", 1),
                self._row("western", 2, debunk=True, reports_it=False)]
        verdict, meta = compute_credibility(rows)
        assert verdict == "unverified" and meta["disputed"] is True
        assert meta["sources"] == 2, "a source that only disputes is not a reporter"


class TestSynthesizeBatch:
    def _armed_report(self, make_source, make_event):
        for perspective in ("ukrainian", "russian", "western"):
            make_event(make_source(perspective), event_type="shelling", lat=48.0, lon=37.0,
                       title=f"Shelling per {perspective}")
        cluster_events()
        (report,) = _reports()
        assert report.llm_status == "pending"
        return report

    def test_synthesizes_and_persists(self, make_source, make_event, monkeypatch):
        from celery_worker.tasks.synthesize import synthesize_reports_batch

        report = self._armed_report(make_source, make_event)
        sent = []

        def fake(items):
            sent.extend(items)
            return {i["idx"]: {"idx": i["idx"], "headline": "Merged headline",
                               "summary": "Merged summary.",
                               "disagreements": "counts differ"} for i in items}

        monkeypatch.setattr(llm, "synthesize_batch", fake)
        result = synthesize_reports_batch()
        assert result["synthesized"] == 1

        entry = sent[0]
        assert entry["verdict"] == report.credibility, "the code's verdict is fed to the prompt"
        assert len(entry["reports"]) == 3
        assert {r["perspective"] for r in entry["reports"]} == {"ukrainian", "russian", "western"}
        assert all("tier" in r for r in entry["reports"])

        db.session.expire_all()
        fresh = db.session.get(SynthesizedReport, report.id)
        assert fresh.llm_status == "done"
        assert fresh.headline_en == "Merged headline"
        assert fresh.summary_en == "Merged summary."
        assert fresh.disagreements_en == "counts differ"
        assert fresh.synthesized_member_count == fresh.member_count

    def test_failure_releases_with_attempt_count(self, make_source, make_event, monkeypatch):
        from celery_worker.tasks.synthesize import synthesize_reports_batch

        report = self._armed_report(make_source, make_event)

        def explode(_items):
            raise llm.LLMError("proxy down")

        monkeypatch.setattr(llm, "synthesize_batch", explode)
        synthesize_reports_batch()
        db.session.expire_all()
        fresh = db.session.get(SynthesizedReport, report.id)
        assert fresh.llm_status == "failed" and fresh.llm_attempts == 1
        assert fresh.llm_claimed_at is None

    def test_transient_failure_spends_no_attempt(self, make_source, make_event, monkeypatch):
        """Same policy as extraction: an outage releases the claim without burning the report's
        attempt cap, so armed reports synthesize themselves once the proxy is back."""
        from celery_worker.tasks.synthesize import synthesize_reports_batch

        report = self._armed_report(make_source, make_event)

        def offline(_items):
            raise llm.LLMTransient("connection refused")

        monkeypatch.setattr(llm, "synthesize_batch", offline)
        for _ in range(5):  # well past MAX_SYNTH_ATTEMPTS
            synthesize_reports_batch()
        db.session.expire_all()
        fresh = db.session.get(SynthesizedReport, report.id)
        assert fresh.llm_status == "pending" and fresh.llm_attempts == 0
        assert fresh.llm_claimed_at is None


class TestDispatcher:
    def test_synthesis_is_a_fourth_kind(self, make_source, make_event, monkeypatch):
        from celery_worker.tasks.dispatch import dispatch_llm

        sent = []
        for kind, path in (
            ("extract", "celery_worker.tasks.extract.llm_extract_batch"),
            ("translate", "celery_worker.tasks.translate.translate_batch"),
            ("summarize", "celery_worker.tasks.summarize.summarize_batch"),
            ("synthesize_reports", "celery_worker.tasks.synthesize.synthesize_reports_batch"),
        ):
            monkeypatch.setattr(f"{path}.apply_async",
                                lambda kind=kind, **kw: sent.append(kind))
        for perspective in ("ukrainian", "russian", "western"):
            make_event(make_source(perspective), event_type="shelling", lat=48.0, lon=37.0)
        cluster_events()
        result = dispatch_llm()
        assert result["synthesize_reports_pending"] == 1
        assert "synthesize_reports" in sent, "an armed report always gets a slot"


class TestSynthesisAPI:
    def _done_report(self, make_source, make_event, monkeypatch):
        for perspective in ("ukrainian", "russian", "western"):
            make_event(make_source(perspective), event_type="shelling", lat=48.0, lon=37.0)
        cluster_events()
        from celery_worker.tasks.synthesize import synthesize_reports_batch

        monkeypatch.setattr(llm, "synthesize_batch", lambda items: {
            i["idx"]: {"idx": i["idx"], "headline": "H", "summary": "S", "disagreements": None}
            for i in items
        })
        synthesize_reports_batch()
        return _reports()[0]

    def test_serves_done_reports_with_members_and_credibility(
            self, client, make_source, make_event, monkeypatch):
        self._done_report(make_source, make_event, monkeypatch)
        data = client.get("/api/synthesis").get_json()
        (item,) = data["items"]
        assert item["headline"] == "H"
        assert item["credibility"]["verdict"] in ("confirmed", "corroborated")
        assert item["credibility"]["sources"] == 3
        assert len(item["members"]) == 3
        assert {m["perspective"] for m in item["members"]} == {"ukrainian", "russian", "western"}
        assert item["centroid"][0] is not None
        assert item["disagreements"] is None

    def test_pending_and_hidden_reports_are_not_served(self, client, make_source, make_event):
        for perspective in ("ukrainian", "russian", "western"):
            make_event(make_source(perspective), event_type="shelling", lat=48.0, lon=37.0)
        cluster_events()  # armed but never synthesized
        assert client.get("/api/synthesis").get_json()["items"] == []

    def test_window_filters_on_last_reported_at(self, client, make_source, make_event,
                                                monkeypatch):
        self._done_report(make_source, make_event, monkeypatch)
        future = _utc(-2).isoformat()
        assert client.get(f"/api/synthesis?from={future}").get_json()["items"] == []
        windowed = client.get(
            f"/api/synthesis?from={_utc(2).isoformat()}&to={_utc(-2).isoformat()}"
        ).get_json()
        assert len(windowed["items"]) == 1

    def test_blackout_masks_the_synthesis(self, client, make_source, make_event, monkeypatch):
        self._done_report(make_source, make_event, monkeypatch)
        db.session.execute(text(
            "INSERT INTO blackout_zones (name, geom, active) VALUES ('z', "
            "ST_GeomFromText('POLYGON((36.5 47.5, 37.5 47.5, 37.5 48.5, 36.5 48.5, 36.5 47.5))',"
            " 4326), true)"
        ))
        db.session.commit()
        from app.services.cache import invalidate

        invalidate("api:synthesis")
        assert client.get("/api/synthesis").get_json()["items"] == []

    def test_etag_roundtrip(self, client, make_source, make_event, monkeypatch):
        self._done_report(make_source, make_event, monkeypatch)
        first = client.get("/api/synthesis")
        etag = first.headers.get("ETag")
        assert etag
        second = client.get("/api/synthesis", headers={"If-None-Match": etag})
        assert second.status_code == 304
