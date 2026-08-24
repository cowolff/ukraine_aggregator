"""Rule-engine scenarios — the spec's acceptance core (PLAN §20 a-e, §13)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import text

from app.extensions import db
from app.models import EvidenceLink, FrontlineClaim
from app.services.rules import evaluate_claims, intake_events

CHASIV_YAR = (48.5906, 37.8306)      # inside the baseline RU polygon of the make_snapshot fixture
DEEP_REAR = (50.4501, 30.5234)       # Kyiv — ~500 km behind the line


def claims():
    return db.session.execute(db.select(FrontlineClaim).order_by(FrontlineClaim.id)).scalars().all()


def run_engine():
    intake = intake_events()
    evaluation = evaluate_claims()
    return intake, evaluation


class TestScenarioA_Corroboration:
    """3.1 — a change is confirmed by ≥2 distinct perspective classes."""

    def test_russian_plus_western_confirms(self, make_source, make_event, make_snapshot):
        make_snapshot()
        ru_source = make_source("russian")
        west_source = make_source("western")
        make_event(ru_source, lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(west_source, lat=CHASIV_YAR[0] + 0.005, lon=CHASIV_YAR[1], claimed_by="ru")

        run_engine()
        rows = claims()
        assert len(rows) == 1, "both reports must join the same claim"
        assert rows[0].status == "confirmed"
        assert rows[0].resolved_by == "rule:corroboration"

    def test_two_sources_same_perspective_stays_pending(self, make_source, make_event, make_snapshot):
        make_snapshot()
        for _ in range(2):
            source = make_source("russian")
            make_event(source, lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")

        run_engine()
        rows = claims()
        assert len(rows) == 1
        assert rows[0].status == "pending"

    def test_neutral_counts_as_western(self, make_source, make_event, make_snapshot):
        """Documented reading of §13: neutral+western is one view class, so it cannot confirm."""
        make_snapshot()
        make_event(make_source("neutral"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(make_source("western"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")

        run_engine()
        assert claims()[0].status == "pending"

    def test_neutral_plus_ukrainian_confirms(self, make_source, make_event, make_snapshot):
        make_snapshot()
        make_event(make_source("neutral"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(make_source("ukrainian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")

        run_engine()
        assert claims()[0].status == "confirmed"

    def test_opposing_directions_do_not_share_a_claim(self, make_source, make_event, make_snapshot):
        make_snapshot()
        make_event(make_source("russian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(make_source("western"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ua")

        run_engine()
        rows = claims()
        assert len(rows) == 2
        assert {r.direction for r in rows} == {"ru_advance", "ua_advance"}
        assert all(r.status == "pending" for r in rows)


class TestScenarioB_GeoProof:
    """3.2 — a single verified geolocation confirms on its own."""

    def test_single_geolocation_proof_confirms(self, make_source, make_event, make_snapshot):
        make_snapshot()
        source = make_source("neutral", tier=1, name="GeoConfirmed")
        make_event(
            source, event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru",
        )

        run_engine()
        rows = claims()
        assert len(rows) == 1
        assert rows[0].status == "confirmed"
        assert rows[0].resolved_by == "rule:geoproof"

    def test_a_proof_that_names_no_side_cannot_open_a_claim(
        self, make_source, make_event, make_snapshot
    ):
        """Verification of a *thing* is not a claim about who controls the ground.

        GeoConfirmed's archive geolocates destroyed vehicles, air-defence sites and factories.
        Those arrive with no `claimed_by`, and treating them as control proofs confirmed 961 claims
        off 7,578 links — roughly 12,400 km² of asserted control taken from records that assert
        nothing of the kind.
        """
        make_snapshot()
        source = make_source("neutral", tier=1, name="GeoConfirmed")
        make_event(
            source, event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by=None,
        )
        run_engine()
        assert claims() == [], "an unassessed geolocation must not invent a frontline claim"

    def test_an_unassessed_proof_cannot_confirm_an_existing_claim_alone(
        self, make_source, make_event, make_snapshot
    ):
        """It may attach as evidence, but rule 3.2 needs a proof that names a side."""
        make_snapshot()
        # One ordinary report opens a pending claim.
        make_event(make_source("russian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        run_engine()
        assert claims()[0].status == "pending"

        # A bulk verification record lands on the same spot.
        make_event(
            make_source("neutral", tier=1, name="GeoConfirmed"),
            event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by=None,
        )
        run_engine()
        db.session.expire_all()
        assert claims()[0].status == "pending", "an unassessed proof does not carry rule 3.2"

    def test_an_assessed_proof_still_confirms_on_its_own(
        self, make_source, make_event, make_snapshot
    ):
        """Rule 3.2 is intact for a proof that actually asserts a side."""
        make_snapshot()
        make_event(
            make_source("neutral", tier=1), event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru",
        )
        run_engine()
        rows = claims()
        assert len(rows) == 1
        assert rows[0].status == "confirmed"
        assert rows[0].direction == "ru_advance"


class TestScenarioC_DebunkAndRevert:
    """3.2 continued — a debunk retracts the proof and reverts the claim."""

    def test_debunk_deactivates_proof_and_reverts_claim(
        self, make_source, make_event, make_snapshot, gazetteer
    ):
        make_snapshot()
        gaz_id = gazetteer["Chasiv Yar"][0]
        proof_source = make_source("neutral", tier=1, name="GeoConfirmed")
        make_event(
            proof_source, event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru", gazetteer_id=gaz_id,
        )
        run_engine()
        claim = claims()[0]
        assert claim.status == "confirmed"

        debunker = make_source("ukrainian", tier=1, name="Debunk desk")
        make_event(
            debunker, event_type="debunk",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by=None, gazetteer_id=gaz_id,
            debunk_target="Часів Яр",
        )
        run_engine()

        db.session.expire_all()
        claim = claims()[0]
        assert claim.status == "reverted"
        assert claim.resolved_by == "rule:debunk", "an actual debunk is attributed as one"
        proof_links = db.session.execute(
            db.select(EvidenceLink).where(EvidenceLink.role == "geolocation_proof")
        ).scalars().all()
        assert all(link.active is False for link in proof_links)

    def test_debunk_matches_by_url(self, make_source, make_event, make_snapshot):
        make_snapshot()
        url = "https://example.test/proof/1"
        proof_source = make_source("neutral", tier=1)
        make_event(
            proof_source, event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru", url=url,
        )
        run_engine()
        assert claims()[0].status == "confirmed"

        make_event(
            make_source("ukrainian"), event_type="debunk",
            lat=DEEP_REAR[0], lon=DEEP_REAR[1], claimed_by=None, debunk_target=url,
        )
        run_engine()
        db.session.expire_all()
        assert claims()[0].status == "reverted"

    def test_reverted_claim_returns_to_confirmed_on_new_evidence(
        self, make_source, make_event, make_snapshot, gazetteer
    ):
        make_snapshot()
        gaz_id = gazetteer["Chasiv Yar"][0]
        make_event(
            make_source("neutral", tier=1), event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru", gazetteer_id=gaz_id,
        )
        run_engine()
        make_event(
            make_source("ukrainian"), event_type="debunk",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by=None, gazetteer_id=gaz_id,
            debunk_target="Часів Яр",
        )
        run_engine()
        db.session.expire_all()
        assert claims()[0].status == "reverted"

        # Two fresh perspectives now report the same change.
        make_event(make_source("russian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(make_source("western"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        run_engine()
        db.session.expire_all()
        assert claims()[0].status == "confirmed"

    def test_unmatched_debunk_is_kept_as_a_feed_item_only(self, make_source, make_event, make_snapshot):
        make_snapshot()
        make_event(
            make_source("ukrainian"), event_type="debunk",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by=None,
            debunk_target="https://example.test/nothing-here",
        )
        run_engine()
        assert claims() == []


class TestScenarioD_DeepStrike:
    """3.3 — deep strikes get icons but never move the line."""

    def test_strike_far_behind_the_line_creates_no_claim(self, make_source, make_event, make_snapshot):
        make_snapshot()
        event = make_event(
            make_source("ukrainian"), event_type="deep_strike",
            lat=DEEP_REAR[0], lon=DEEP_REAR[1], claimed_by="ru",
        )
        intake, _ = run_engine()
        assert claims() == []
        assert intake.deep_strikes == 1
        # The icon itself survives — it is still a visible, placed event.
        placed = db.session.execute(
            text("SELECT geom IS NOT NULL AS placed, visible FROM extracted_events WHERE id = :i"),
            {"i": event.id},
        ).first()
        assert placed.placed and placed.visible

    def test_advance_reported_far_behind_the_line_is_treated_as_a_deep_strike(
        self, make_source, make_event, make_snapshot
    ):
        make_snapshot()
        make_event(
            make_source("russian"), event_type="frontline_advance",
            lat=DEEP_REAR[0], lon=DEEP_REAR[1], claimed_by="ru",
        )
        make_event(
            make_source("western"), event_type="frontline_advance",
            lat=DEEP_REAR[0], lon=DEEP_REAR[1], claimed_by="ru",
        )
        intake, _ = run_engine()
        assert claims() == [], "distance > DEEP_STRIKE_KM must never open a claim"
        assert intake.deep_strikes == 2

    def test_debunk_far_behind_the_line_still_enters_matching(
        self, make_source, make_event, make_snapshot
    ):
        """The documented exception: debunks are matched regardless of distance."""
        make_snapshot()
        url = "https://example.test/proof/9"
        make_event(
            make_source("neutral", tier=1), event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru", url=url,
        )
        run_engine()
        make_event(
            make_source("ukrainian"), event_type="debunk",
            lat=DEEP_REAR[0], lon=DEEP_REAR[1], claimed_by=None, debunk_target=url,
        )
        intake, _ = run_engine()
        assert intake.debunks_matched == 1
        db.session.expire_all()
        assert claims()[0].status == "reverted"


class TestDigestReports:
    """A daily roundup names dozens of settlements and asserts a change at none of them."""

    def _digest(self, source, places, *, claimed_by="ru", title="Сводка за 24 августа"):
        """One news item that names `places` settlements — a situation summary."""
        import datetime as dt

        from sqlalchemy import text as sql_text

        from app.models import ExtractedEvent, NewsItem, content_hash

        item = NewsItem(
            source_id=source.id, content_hash=content_hash(title, str(places)),
            title=title, body="…", llm_status="done",
            published_at=dt.datetime.now(dt.timezone.utc),
        )
        db.session.add(item)
        db.session.flush()
        for i in range(places):
            event = ExtractedEvent(
                news_item_id=item.id, event_type="frontline_advance",
                claimed_by=claimed_by, confidence=0.9, coord_source="explicit_coords",
                occurred_at=item.published_at,
            )
            db.session.add(event)
            db.session.flush()
            # All at the same spot, so any claim they formed would be a single one.
            db.session.execute(
                sql_text(
                    "UPDATE extracted_events SET geom = ST_SetSRID(ST_MakePoint(:lon,:lat),4326) "
                    "WHERE id = :eid"
                ),
                {"lon": CHASIV_YAR[1], "lat": CHASIV_YAR[0], "eid": event.id},
            )
        db.session.commit()
        return item

    def test_a_digest_opens_no_claim(self, make_source, make_snapshot):
        make_snapshot()
        self._digest(make_source("russian"), places=20)
        intake, _ = run_engine()
        assert claims() == [], "a 20-settlement roundup asserts no specific change"
        assert intake.digests_skipped == 20

    def test_a_focused_report_still_opens_a_claim(self, make_source, make_snapshot):
        make_snapshot()
        self._digest(make_source("russian"), places=2, title="ЗС РФ зайшли до Часового Яру")
        run_engine()
        assert len(claims()) == 1, "a report naming a couple of places is a real report"

    def test_two_digests_do_not_corroborate_each_other(self, make_source, make_snapshot):
        """The core failure: two daily bulletins listing the same town are not corroboration."""
        make_snapshot()
        self._digest(make_source("russian"), places=20, title="Сводка МО РФ")
        self._digest(make_source("ukrainian"), places=20, title="Генштаб: зведення за добу")
        run_engine()
        assert claims() == []

    def test_a_digest_cannot_confirm_a_real_claim_it_merely_mentions(
        self, make_source, make_event, make_snapshot
    ):
        make_snapshot()
        # A focused Russian report opens a pending claim.
        make_event(make_source("russian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        run_engine()
        assert claims()[0].status == "pending"

        # A Ukrainian daily roundup happens to list the same town among twenty.
        self._digest(make_source("ukrainian"), places=20, title="Генштаб: зведення за добу")
        run_engine()
        db.session.expire_all()
        assert claims()[0].status == "pending", "a roundup is not a second perspective"


class TestRevertAttribution:
    """Why a claim was reverted has to be recorded honestly — the admin reads it."""

    def test_a_claim_still_confirmed_for_a_different_reason_is_reattributed(
        self, make_source, make_event, make_snapshot
    ):
        make_snapshot()
        # Confirmed by an assessed geo-proof.
        make_event(
            make_source("neutral", tier=1), event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru",
        )
        run_engine()
        assert claims()[0].resolved_by == "rule:geoproof"

        # The proof is retracted, but two perspectives now report the same change.
        db.session.execute(
            text("UPDATE evidence_links SET active = false WHERE role = 'geolocation_proof'")
        )
        db.session.commit()
        make_event(make_source("russian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(make_source("ukrainian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        run_engine()

        db.session.expire_all()
        claim = claims()[0]
        assert claim.status == "confirmed"
        assert claim.resolved_by == "rule:corroboration", "the recorded reason must stay true"

    def test_a_manual_confirmation_is_never_reattributed(
        self, make_source, make_event, make_snapshot
    ):
        make_snapshot()
        make_event(make_source("russian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(make_source("western"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        run_engine()
        db.session.execute(
            text("UPDATE frontline_claims SET resolved_by = 'admin:someone'")
        )
        db.session.commit()

        evaluate_claims()
        db.session.expire_all()
        assert claims()[0].resolved_by == "admin:someone"

    def test_admin_deactivating_evidence_is_not_recorded_as_a_debunk(
        self, make_source, make_event, make_snapshot
    ):
        make_snapshot()
        make_event(
            make_source("neutral", tier=1), event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru",
        )
        run_engine()
        assert claims()[0].status == "confirmed"

        # An admin retracts the proof; no debunk report was ever filed.
        db.session.execute(text("UPDATE evidence_links SET active = false"))
        db.session.commit()
        evaluate_claims()

        db.session.expire_all()
        claim = claims()[0]
        assert claim.status == "reverted"
        assert claim.resolved_by == "rule:evidence_retracted"


class TestScenarioE_Tier3:
    """Tier-3 sources alone can never confirm anything."""

    def test_two_tier3_perspectives_do_not_confirm(self, make_source, make_event, make_snapshot):
        make_snapshot()
        make_event(make_source("russian", tier=3), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(make_source("western", tier=3), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")

        run_engine()
        assert claims()[0].status == "pending"

    def test_tier3_geolocation_proof_does_not_confirm(self, make_source, make_event, make_snapshot):
        make_snapshot()
        make_event(
            make_source("neutral", tier=3), event_type="geolocation_proof",
            lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru",
        )
        run_engine()
        assert claims()[0].status == "pending"

    def test_tier3_plus_credible_source_confirms(self, make_source, make_event, make_snapshot):
        make_snapshot()
        make_event(make_source("russian", tier=3), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(make_source("western", tier=1), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(make_source("ukrainian", tier=2), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")

        run_engine()
        assert claims()[0].status == "confirmed"


class TestLowConfidenceAndStaleness:
    def test_low_confidence_events_are_not_evidence(self, make_source, make_event, make_snapshot):
        make_snapshot()
        make_event(
            make_source("russian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1],
            claimed_by="ru", confidence=0.2,
        )
        make_event(
            make_source("western"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1],
            claimed_by="ru", confidence=0.2,
        )
        run_engine()
        assert claims() == [], "sub-threshold extractions must not even open a claim"

    def test_stale_pending_claim_is_rejected(self, make_source, make_event, make_snapshot):
        make_snapshot()
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)
        make_event(
            make_source("russian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1],
            claimed_by="ru", published_at=old,
        )
        run_engine()
        claim = claims()[0]
        assert claim.status == "pending"

        db.session.execute(
            text("UPDATE frontline_claims SET created_at = :old WHERE id = :cid"),
            {"old": old, "cid": claim.id},
        )
        db.session.commit()
        evaluate_claims()
        db.session.expire_all()
        claim = claims()[0]
        assert claim.status == "rejected"
        assert claim.resolved_by == "rule:stale"


class TestIntakeIdempotence:
    def test_events_are_only_taken_in_once(self, make_source, make_event, make_snapshot):
        make_snapshot()
        make_event(make_source("russian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        first = intake_events()
        second = intake_events()
        assert first.events_seen == 1
        assert second.events_seen == 0
        assert len(claims()) == 1

    def test_no_snapshot_yet_still_opens_claims(self, make_source, make_event):
        """Before the first build there is no line to measure against; intake must not stall."""
        make_event(make_source("russian"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        make_event(make_source("western"), lat=CHASIV_YAR[0], lon=CHASIV_YAR[1], claimed_by="ru")
        run_engine()
        assert len(claims()) == 1
        assert claims()[0].status == "confirmed"
