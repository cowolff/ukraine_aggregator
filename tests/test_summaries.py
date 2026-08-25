"""English summaries: one general summary per item, one focused summary per map location."""
from __future__ import annotations

from sqlalchemy import text

from app.extensions import db
from app.models import ExtractedEvent, NewsItem, content_hash
from app.services import llm


class TestSummaryBatch:
    def _item(self, source, title, body, *, llm_status="done", status="pending", relevant=True):
        item = NewsItem(
            source_id=source.id, content_hash=content_hash(title, body or ""),
            title=title, body=body, llm_status=llm_status,
            translation_status="skipped", summary_status=status,
        )
        db.session.add(item)
        db.session.flush()
        # A judged relevant item always carries at least one event row (the extraction invariant);
        # a judged item without one is off-topic and gets retired instead of summarised.
        if relevant and llm_status == "done":
            db.session.add(ExtractedEvent(news_item_id=item.id, event_type="other"))
        db.session.commit()
        return item

    def _event(self, item, place):
        event = ExtractedEvent(
            news_item_id=item.id, event_type="shelling", place_name_raw=place,
        )
        db.session.add(event)
        db.session.commit()
        return event

    def test_location_summaries_fan_out_to_every_marker_of_that_place(self, make_source,
                                                                       monkeypatch):
        from celery_worker.tasks.summarize import summarize_batch

        source = make_source("ukrainian")
        item = self._item(source, "Strikes reported",
                          "Strikes on Pokrovsk and Kostiantynivka overnight.")
        first = self._event(item, "Pokrovsk")
        second = self._event(item, "Pokrovsk")        # second marker, same settlement
        other = self._event(item, "Kostiantynivka")
        nameless = self._event(item, None)            # coordinate-only marker

        sent = []

        def fake(items):
            sent.extend(items)
            return {
                i["idx"]: {
                    "idx": i["idx"],
                    "summary": "General overnight strike roundup.",
                    "locations": [
                        {"name": n, "summary": f"What happened at {n}."}
                        for n in i["locations"]
                    ],
                }
                for i in items
            }

        monkeypatch.setattr(llm, "summarize_batch", fake)
        result = summarize_batch()
        assert result["summarized"] == 1
        assert result["location_summaries"] == 3, "one shared entry covers both Pokrovsk markers"
        assert sent[0]["locations"] == ["Pokrovsk", "Kostiantynivka"], \
            "distinct place names, in event order — one summary is requested per name, not per marker"

        db.session.expire_all()
        fresh = db.session.get(NewsItem, item.id)
        assert fresh.summary_en == "General overnight strike roundup."
        assert fresh.summary_status == "done"
        summaries = {e.id: e.summary_en for e in fresh.events}
        assert summaries[first.id] == summaries[second.id] == "What happened at Pokrovsk."
        assert summaries[other.id] == "What happened at Kostiantynivka."
        assert summaries[nameless.id] is None, "no place name → the general summary serves"

    def test_headline_only_items_are_skipped_without_calling_the_proxy(self, make_source,
                                                                       monkeypatch):
        from celery_worker.tasks.summarize import summarize_batch

        source = make_source("western")
        self._item(source, "Bare headline", None)
        self._item(source, "Copied headline", "  Copied   headline ")

        def explode(_items):
            raise AssertionError("an item without content must not reach the proxy")

        monkeypatch.setattr(llm, "summarize_batch", explode)
        result = summarize_batch()
        assert result == {"batch": 0, "skipped": 2}
        statuses = db.session.execute(text("SELECT summary_status FROM news_items")).scalars().all()
        assert statuses == ["skipped", "skipped"]

    def test_judged_irrelevant_items_are_retired_without_calling_the_proxy(self, make_source,
                                                                           monkeypatch):
        """Extraction judged it off-topic → the feed never shows it, so no summary is needed."""
        from celery_worker.tasks.summarize import summarize_batch

        source = make_source("western")
        self._item(source, "World news", "Iran and the NPT, nothing about Ukraine.",
                   relevant=False)

        def explode(_items):
            raise AssertionError("an off-topic item must not reach the proxy")

        monkeypatch.setattr(llm, "summarize_batch", explode)
        result = summarize_batch()
        assert result == {"batch": 0, "skipped": 1}
        assert db.session.execute(
            text("SELECT summary_status FROM news_items")
        ).scalar() == "skipped"

    def test_waits_until_extraction_has_settled(self, make_source, monkeypatch):
        """The location list must be the map's location list, so extraction goes first."""
        from celery_worker.tasks.summarize import summarize_batch

        source = make_source("western")
        self._item(source, "Fresh item", "Body text here", llm_status="pending")

        def explode(_items):
            raise AssertionError("an unextracted item must not be summarised yet")

        monkeypatch.setattr(llm, "summarize_batch", explode)
        assert summarize_batch()["batch"] == 0
        assert db.session.execute(
            text("SELECT summary_status FROM news_items")
        ).scalar() == "pending"

    def test_proxy_failure_leaves_the_item_retryable(self, make_source, monkeypatch):
        from celery_worker.tasks.summarize import summarize_batch

        source = make_source("russian")
        self._item(source, "Удар по Харькову", "Подробности удара")

        monkeypatch.setattr(
            llm, "summarize_batch",
            lambda _i: (_ for _ in ()).throw(llm.LLMError("proxy 503")),
        )
        assert "error" in summarize_batch()
        row = db.session.execute(
            text("SELECT summary_status, summary_claimed_at FROM news_items")
        ).first()
        assert row.summary_status == "pending"
        assert row.summary_claimed_at is None, "a released claim carries no timestamp"

    def test_concurrent_batches_do_not_claim_the_same_item(self, make_source):
        from celery_worker.tasks.summarize import _claim

        source = make_source("russian")
        for i in range(30):
            self._item(source, f"Удар номер {i}", f"текст {i}")

        first, _ = _claim(None)
        second, _ = _claim(None)
        assert first, "the first batch should get work"
        assert {i.id for i in first}.isdisjoint({i.id for i in second})


class TestApiExposesSummaries:
    def test_news_carries_general_and_per_event_summaries(self, client, make_source, make_event):
        event = make_event(make_source("ukrainian"), lat=48.5, lon=37.5)
        db.session.execute(
            text("UPDATE news_items SET summary_en='General summary' WHERE id=:i"),
            {"i": event.news_item_id},
        )
        db.session.execute(
            text("UPDATE extracted_events SET summary_en='At the spot' WHERE id=:e"),
            {"e": event.id},
        )
        db.session.commit()

        payload = client.get("/api/news").get_json()["items"][0]
        assert payload["summary"] == "General summary"
        assert payload["events"][0]["summary"] == "At the spot"

    def test_map_points_prefer_the_location_summary_over_the_general_one(self, client,
                                                                         make_source, make_event):
        source = make_source("russian")
        focused = make_event(source, lat=48.5, lon=37.5)
        general = make_event(source, lat=49.0, lon=36.0)
        db.session.execute(text("UPDATE news_items SET summary_en='General item summary'"))
        db.session.execute(
            text("UPDATE extracted_events SET summary_en='Focused on this place' WHERE id=:e"),
            {"e": focused.id},
        )
        db.session.commit()

        features = client.get("/api/events").get_json()["features"]
        by_id = {f["properties"]["id"]: f["properties"]["summary"] for f in features}
        assert by_id[focused.id] == "Focused on this place"
        assert by_id[general.id] == "General item summary", \
            "a marker without its own location summary falls back to the item's general one"
