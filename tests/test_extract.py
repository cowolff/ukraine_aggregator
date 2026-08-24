"""LLM prefilter, prompt plumbing and extraction postprocessing (PLAN §11).

The proxy is never contacted: llm.extract_batch is monkeypatched throughout.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models import ExtractedEvent, NewsItem, content_hash
from app.services import llm
from celery_worker.tasks.extract import _persist, llm_extract_batch


class TestPrefilter:
    @pytest.mark.parametrize(
        "title,body",
        [
            ("Russian forces captured Novoselivka", "the assault continues"),
            ("ЗСУ звільнили село", "просунулися на 2 км"),
            ("Обстріл Харкова", "влучання КАБів"),
            ("Drone strike on refinery", "shahed hit the facility"),
            ("Geolocated: new position", "confirmed footage from Chasiv Yar"),
            ("This footage is AI-generated", "the debunk thread explains"),
        ],
    )
    def test_relevant_text_passes(self, title, body):
        assert llm.is_war_relevant(title, body) is True

    @pytest.mark.parametrize(
        "title,body",
        [
            ("Recipe for borscht", "add beetroot and simmer"),
            ("Quarterly earnings report", "revenue rose 4 percent"),
            ("", ""),
            (None, None),
        ],
    )
    def test_irrelevant_text_is_filtered(self, title, body):
        assert llm.is_war_relevant(title, body) is False

    def test_prefiltered_items_are_marked_skipped_without_calling_the_proxy(self, make_source, monkeypatch):
        source = make_source("western")
        called = {"n": 0}

        def explode(_items):
            called["n"] += 1
            raise AssertionError("the proxy must not be called for irrelevant items")

        monkeypatch.setattr(llm, "extract_batch", explode)
        for i in range(3):
            db.session.add(
                NewsItem(
                    source_id=source.id, content_hash=content_hash(f"cake {i}", "sugar"),
                    title=f"cake {i}", body="sugar and flour", llm_status="pending",
                )
            )
        db.session.commit()

        result = llm_extract_batch()
        assert called["n"] == 0
        assert result["skipped"] == 3
        statuses = db.session.execute(text("SELECT DISTINCT llm_status FROM news_items")).scalars().all()
        assert statuses == ["skipped"]


class TestJSONParsing:
    def test_plain_json(self):
        assert llm.parse_json_loose('{"items":[{"idx":0}]}') == {"items": [{"idx": 0}]}

    def test_fenced_json(self):
        assert llm.parse_json_loose('```json\n{"items":[]}\n```') == {"items": []}

    def test_json_with_surrounding_prose(self):
        assert llm.parse_json_loose('Sure! {"items":[{"idx":1}]} hope that helps') == {
            "items": [{"idx": 1}]
        }

    def test_unparseable_raises(self):
        with pytest.raises(llm.LLMError):
            llm.parse_json_loose("no json at all")
        with pytest.raises(llm.LLMError):
            llm.parse_json_loose("")

    def test_prompt_is_stable_and_carries_the_schema(self):
        messages = llm.build_messages([{"idx": 0, "title": "t", "body": "b"}])
        assert messages[0]["role"] == "system"
        assert "frontline_advance" in messages[0]["content"]
        assert "NEVER invent coordinates" in messages[0]["content"]
        assert '"idx": 0' in messages[1]["content"] or '"idx":0' in messages[1]["content"]

    def test_truncation_respects_the_configured_limit(self):
        from app.config import settings

        long_text = "x" * (settings.llm_max_body_chars + 500)
        out = llm.truncate(long_text)
        assert len(out) <= settings.llm_max_body_chars + 2
        assert llm.truncate(None) == ""


class TestPersistence:
    def _item(self, source, title="Assault on Chasiv Yar", body="fighting reported"):
        item = NewsItem(
            source_id=source.id, content_hash=content_hash(title, body),
            title=title, body=body, llm_status="pending",
            published_at=dt.datetime.now(dt.timezone.utc),
        )
        db.session.add(item)
        db.session.commit()
        return item

    def events_for(self, item):
        return db.session.execute(
            db.select(ExtractedEvent).where(ExtractedEvent.news_item_id == item.id)
        ).scalars().all()

    def test_irrelevant_result_creates_nothing(self, make_source):
        item = self._item(make_source("western"))
        assert _persist(item, {"relevant": False}) == 0
        assert self.events_for(item) == []

    def test_one_event_per_location(self, make_source):
        item = self._item(make_source("russian"))
        created = _persist(item, {
            "relevant": True, "event_type": "frontline_claim", "claimed_by": "ru",
            "confidence": 0.8,
            "locations": [
                {"name": "Часів Яр", "lat": None, "lon": None, "oblast": "Донецька"},
                {"name": "Бахмут", "lat": None, "lon": None, "oblast": "Донецька"},
            ],
        })
        assert created == 2
        names = sorted(e.place_name_raw for e in self.events_for(item))
        assert names == ["Бахмут", "Часів Яр"]
        assert all(e.geom is None for e in self.events_for(item)), "names alone must stay unplaced"

    def test_relevant_item_without_locations_becomes_a_feed_only_event(self, make_source):
        item = self._item(make_source("western"))
        assert _persist(item, {
            "relevant": True, "event_type": "other", "confidence": 0.6, "locations": [],
        }) == 1
        event = self.events_for(item)[0]
        assert event.geom is None and event.place_name_raw is None

    def test_llm_coordinates_inside_the_bbox_are_kept(self, make_source):
        item = self._item(make_source("neutral"))
        _persist(item, {
            "relevant": True, "event_type": "geolocation_proof", "confidence": 0.95,
            "locations": [{"name": "position", "lat": 48.59, "lon": 37.83, "oblast": None}],
        })
        event = self.events_for(item)[0]
        assert event.coord_source == "explicit_coords"
        placed = db.session.execute(
            text("SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon FROM extracted_events WHERE id=:i"),
            {"i": event.id},
        ).first()
        assert placed.lat == pytest.approx(48.59, abs=1e-4)

    def test_llm_coordinates_outside_ukraine_are_discarded(self, make_source):
        """The sanity bbox is the guard against a hallucinated coordinate landing on the map."""
        item = self._item(make_source("neutral"))
        _persist(item, {
            "relevant": True, "event_type": "geolocation_proof", "confidence": 0.9,
            "locations": [{"name": "somewhere", "lat": 5.0, "lon": 5.0, "oblast": None}],
        })
        event = self.events_for(item)[0]
        assert event.geom is None
        assert event.coord_source is None

    def test_regex_coordinates_override_the_llm(self, make_source):
        source = make_source("russian")
        item = self._item(source, title="Position held", body="verified at 48.5906, 37.8306 today")
        _persist(item, {
            "relevant": True, "event_type": "geolocation_proof", "confidence": 0.9,
            # The model reports a different point; the text's own numbers must win.
            "locations": [{"name": "spot", "lat": 50.0, "lon": 30.0, "oblast": None}],
        })
        event = self.events_for(item)[0]
        placed = db.session.execute(
            text("SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon FROM extracted_events WHERE id=:i"),
            {"i": event.id},
        ).first()
        assert placed.lat == pytest.approx(48.5906, abs=1e-4)
        assert placed.lon == pytest.approx(37.8306, abs=1e-4)

    def test_repeated_locations_in_one_response_collapse(self, make_source):
        """One response listing the same place repeatedly must not become several events."""
        item = self._item(make_source("russian"))
        created = _persist(item, {
            "relevant": True, "event_type": "shelling", "confidence": 0.8,
            "locations": [
                {"name": "Часів Яр", "oblast": "Донецька"},
                {"name": "часів яр", "oblast": "Донецька"},   # same place, different casing
                {"name": "Часів Яр", "oblast": "Донецька"},
                {"name": "Бахмут", "oblast": "Донецька"},
            ],
        })
        assert created == 2
        assert sorted(e.place_name_raw for e in self.events_for(item)) == ["Бахмут", "Часів Яр"]

    def test_repeated_empty_locations_collapse_to_one(self, make_source):
        item = self._item(make_source("russian"))
        created = _persist(item, {
            "relevant": True, "event_type": "other", "confidence": 0.5,
            "locations": [{"name": None} for _ in range(18)],
        })
        assert created == 1

    def test_unknown_event_type_falls_back_to_other(self, make_source):
        item = self._item(make_source("western"))
        _persist(item, {"relevant": True, "event_type": "invented_type", "locations": []})
        assert self.events_for(item)[0].event_type == "other"

    def test_bad_claimed_by_and_confidence_are_coerced(self, make_source):
        item = self._item(make_source("western"))
        _persist(item, {
            "relevant": True, "event_type": "shelling", "claimed_by": "martians",
            "confidence": "high", "locations": [],
        })
        event = self.events_for(item)[0]
        assert event.claimed_by is None
        assert event.confidence is None

    def test_malformed_locations_do_not_crash(self, make_source):
        item = self._item(make_source("western"))
        created = _persist(item, {
            "relevant": True, "event_type": "other", "locations": ["not a dict", None, 42],
        })
        assert created == 1  # falls back to a single unplaced event


class TestBatchTask:
    def test_batch_marks_done_and_creates_events(self, make_source, monkeypatch):
        source = make_source("russian")
        titles = ["Assault on Chasiv Yar", "Shelling of Bakhmut"]
        for title in titles:
            db.session.add(
                NewsItem(
                    source_id=source.id, content_hash=content_hash(title, "body"),
                    title=title, body="fighting reported near the settlement", llm_status="pending",
                )
            )
        db.session.commit()

        def fake_extract(items):
            return {
                i["idx"]: {
                    "idx": i["idx"], "relevant": True, "event_type": "frontline_claim",
                    "claimed_by": "ru", "confidence": 0.7,
                    "locations": [{"name": "Часів Яр", "oblast": "Донецька"}],
                }
                for i in items
            }

        monkeypatch.setattr(llm, "extract_batch", fake_extract)
        result = llm_extract_batch()
        assert result["batch"] == 2
        assert result["events"] == 2
        statuses = db.session.execute(text("SELECT DISTINCT llm_status FROM news_items")).scalars().all()
        assert statuses == ["done"]

    def test_proxy_failure_leaves_items_retryable(self, make_source, monkeypatch):
        source = make_source("russian")
        db.session.add(
            NewsItem(
                source_id=source.id, content_hash=content_hash("Assault", "body"),
                title="Assault on Chasiv Yar", body="shelling reported", llm_status="pending",
            )
        )
        db.session.commit()

        def boom(_items):
            raise llm.LLMError("proxy 503")

        monkeypatch.setattr(llm, "extract_batch", boom)
        result = llm_extract_batch()
        assert "error" in result
        row = db.session.execute(text("SELECT llm_status, llm_attempts FROM news_items")).first()
        assert row.llm_status == "pending" and row.llm_attempts == 1

    def test_items_fail_permanently_after_max_attempts(self, make_source, monkeypatch):
        source = make_source("russian")
        db.session.add(
            NewsItem(
                source_id=source.id, content_hash=content_hash("Assault", "body"),
                title="Assault on Chasiv Yar", body="shelling reported", llm_status="pending",
            )
        )
        db.session.commit()
        monkeypatch.setattr(llm, "extract_batch", lambda _i: (_ for _ in ()).throw(llm.LLMError("nope")))

        for _ in range(4):
            llm_extract_batch()
        row = db.session.execute(text("SELECT llm_status, llm_attempts FROM news_items")).first()
        assert row.llm_status == "failed"
        assert row.llm_attempts == 3

    def test_concurrent_batches_never_claim_the_same_item(self, make_source):
        """Two workers claiming at once must partition the queue, not duplicate it.

        Regression test for the concurrency race found under load: `SELECT ... LIMIT` takes no
        locks, so every parallel extraction worker picked the same pending rows and extracted them
        repeatedly (314 duplicated items in a 6-way run).
        """
        import threading

        from sqlalchemy import create_engine, text as sql_text
        from sqlalchemy.orm import sessionmaker

        from app.config import settings
        from celery_worker.tasks.extract import _claim

        source = make_source("russian")
        for i in range(40):
            title = f"Assault on settlement {i}"
            db.session.add(
                NewsItem(
                    source_id=source.id, content_hash=content_hash(title, str(i)),
                    title=title, body="shelling and advance reported", llm_status="pending",
                )
            )
        db.session.commit()

        # A genuinely separate connection, so the two claims race in the database, not in one session.
        engine = create_engine(settings.database_url)
        Session = sessionmaker(bind=engine)
        other_claimed: list[int] = []
        barrier = threading.Barrier(2, timeout=20)

        def rival():
            session = Session()
            try:
                barrier.wait()
                rows = session.execute(
                    sql_text(
                        "SELECT id FROM news_items WHERE llm_status = 'pending' "
                        "ORDER BY llm_attempts, id DESC LIMIT :n FOR UPDATE SKIP LOCKED"
                    ),
                    {"n": settings.llm_batch_size * 3},
                ).scalars().all()
                session.execute(
                    sql_text("UPDATE news_items SET llm_status='processing' WHERE id = ANY(:ids)"),
                    {"ids": list(rows)},
                )
                session.commit()
                other_claimed.extend(rows)
            finally:
                session.close()

        thread = threading.Thread(target=rival)
        thread.start()
        barrier.wait()
        mine, _skipped = _claim(None)
        thread.join(timeout=20)
        engine.dispose()

        mine_ids = {item.id for item in mine}
        assert mine_ids, "this worker should still get a batch"
        assert other_claimed, "the rival worker should also get a batch"
        assert mine_ids.isdisjoint(other_claimed), (
            f"both workers claimed {mine_ids & set(other_claimed)}"
        )

    def test_claimed_items_are_marked_processing(self, make_source):
        from celery_worker.tasks.extract import _claim

        source = make_source("russian")
        for i in range(3):
            title = f"Shelling of settlement {i}"
            db.session.add(
                NewsItem(
                    source_id=source.id, content_hash=content_hash(title, str(i)),
                    title=title, body="artillery strike reported", llm_status="pending",
                )
            )
        db.session.commit()

        batch, _ = _claim(None)
        assert batch
        statuses = db.session.execute(
            text("SELECT DISTINCT llm_status FROM news_items WHERE id = ANY(:ids)"),
            {"ids": [i.id for i in batch]},
        ).scalars().all()
        assert statuses == ["processing"]

    def test_stale_claims_are_released_but_live_ones_are_not(self, make_source):
        """Recovery must free a dead worker's claim without touching a running batch.

        Keying this off `fetched_at` (the ingestion time) instead of the claim time would release
        a batch that started one second ago, re-creating the duplicate processing that claiming
        prevents.
        """
        import datetime as dt

        from celery_worker.tasks.maintenance import maintenance

        source = make_source("russian")
        ids = []
        for i in range(2):
            title = f"Assault on settlement {i}"
            item = NewsItem(
                source_id=source.id, content_hash=content_hash(title, str(i)),
                title=title, body="advance reported", llm_status="processing",
                # Ingested long ago — the old, wrong staleness signal.
                fetched_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2),
            )
            db.session.add(item)
            db.session.flush()
            ids.append(item.id)
        # First is genuinely abandoned; second was claimed a moment ago.
        db.session.execute(
            text("UPDATE news_items SET llm_claimed_at = now() - interval '2 hours' WHERE id = :i"),
            {"i": ids[0]},
        )
        db.session.execute(
            text("UPDATE news_items SET llm_claimed_at = now() WHERE id = :i"), {"i": ids[1]}
        )
        db.session.commit()

        result = maintenance()
        assert result["claims_released"] == 1
        db.session.expire_all()
        statuses = {
            row.id: row.llm_status
            for row in db.session.execute(
                text("SELECT id, llm_status FROM news_items WHERE id = ANY(:ids)"), {"ids": ids}
            )
        }
        assert statuses[ids[0]] == "pending", "an abandoned claim must be released"
        assert statuses[ids[1]] == "processing", "a live claim must be left alone"

    def test_empty_queue_is_a_no_op(self):
        assert llm_extract_batch() == {"batch": 0, "skipped": 0}
