"""English rendering for display: what gets translated, and how it reaches the API."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models import NewsItem, content_hash
from app.services import llm


class TestNeedsTranslation:
    @pytest.mark.parametrize(
        "title,body,language,expected",
        [
            # Cyrillic always needs translating, whatever the catalogue says.
            ("Обстріл Харкова", "влучання КАБів", "Ukrainian", True),
            ("Удар по Одессе", "", "Russian", True),
            ("Обстріл", "", None, True),
            ("Обстріл", "", "English", True),
            # Latin script: the catalogued language decides, because Latin ≠ English.
            ("Rosja zaatakowała", "nocne nalot", "Polish", True),
            ("Russian forces advanced", "near Pokrovsk", "English", False),
            ("Mixed feed headline", "", "Ukrainian, English", False),
            # Nothing recorded and no Cyrillic: assume English rather than spend tokens.
            ("Some headline", "body", None, False),
            # Nothing to translate.
            ("", "", "Ukrainian", False),
            (None, None, "Russian", False),
        ],
    )
    def test_decision(self, title, body, language, expected):
        assert llm.needs_translation(title, body, language) is expected


class TestTranslationBatch:
    def _item(self, source, title, body="текст", status="pending"):
        # llm_status "pending": translation runs in parallel with extraction, so the normal case
        # is an item not yet judged for relevance. (A judged item needs event rows to translate —
        # see test_judged_irrelevant_items_are_retired.)
        item = NewsItem(
            source_id=source.id, content_hash=content_hash(title, body),
            title=title, body=body, llm_status="pending", translation_status=status,
        )
        db.session.add(item)
        db.session.commit()
        return item

    def test_translates_and_keeps_the_original(self, make_source, monkeypatch):
        from celery_worker.tasks.translate import translate_batch

        source = make_source("ukrainian", meta={"language": "Ukrainian"})
        item = self._item(source, "Обстріл Покровська", "Російські війська атакували місто")

        monkeypatch.setattr(
            llm, "translate_batch",
            lambda items: {
                i["idx"]: {"idx": i["idx"], "title": "Shelling of Pokrovsk",
                           "body": "Russian forces attacked the city"}
                for i in items
            },
        )
        result = translate_batch()
        assert result["translated"] == 1

        db.session.expire_all()
        fresh = db.session.get(NewsItem, item.id)
        assert fresh.title_en == "Shelling of Pokrovsk"
        assert fresh.title == "Обстріл Покровська", "the original must never be overwritten"
        assert fresh.translation_status == "done"

    def test_english_sources_are_skipped_without_calling_the_proxy(self, make_source, monkeypatch):
        from celery_worker.tasks.translate import translate_batch

        source = make_source("western", meta={"language": "English"})
        self._item(source, "Russian forces advanced near Pokrovsk", "more detail here")

        def explode(_items):
            raise AssertionError("an English item must not reach the proxy")

        monkeypatch.setattr(llm, "translate_batch", explode)
        result = translate_batch()
        assert result["skipped"] == 1
        assert db.session.execute(
            text("SELECT translation_status FROM news_items")
        ).scalar() == "skipped"

    def test_proxy_failure_leaves_the_item_retryable(self, make_source, monkeypatch):
        from celery_worker.tasks.translate import translate_batch

        source = make_source("russian", meta={"language": "Russian"})
        self._item(source, "Удар по Харькову")

        monkeypatch.setattr(
            llm, "translate_batch",
            lambda _i: (_ for _ in ()).throw(llm.LLMError("proxy 503")),
        )
        assert "error" in translate_batch()
        row = db.session.execute(
            text("SELECT translation_status, translation_claimed_at FROM news_items")
        ).first()
        assert row.translation_status == "pending"
        assert row.translation_claimed_at is None, "a released claim carries no timestamp"

    def test_judged_irrelevant_items_are_retired_without_calling_the_proxy(self, make_source,
                                                                           monkeypatch):
        """Extraction judged it off-topic (done, zero events) → the feed never shows it, so
        translating it would be pure token waste."""
        from celery_worker.tasks.translate import translate_batch

        source = make_source("russian", meta={"language": "Russian"})
        item = self._item(source, "Мировые новости не о войне")
        db.session.execute(
            text("UPDATE news_items SET llm_status='done' WHERE id=:i"), {"i": item.id}
        )
        db.session.commit()

        def explode(_items):
            raise AssertionError("an off-topic item must not reach the proxy")

        monkeypatch.setattr(llm, "translate_batch", explode)
        result = translate_batch()
        assert result["skipped"] == 1
        assert db.session.execute(
            text("SELECT translation_status FROM news_items")
        ).scalar() == "skipped"

    def test_concurrent_batches_do_not_claim_the_same_item(self, make_source, monkeypatch):
        from celery_worker.tasks.translate import _claim

        source = make_source("russian", meta={"language": "Russian"})
        for i in range(30):
            self._item(source, f"Удар номер {i}", f"текст {i}")

        first, _ = _claim(None)
        # The first claim is committed as 'processing', so a second pass must not see those rows.
        second, _ = _claim(None)
        assert first, "the first batch should get work"
        assert {i.id for i in first}.isdisjoint({i.id for i in second})


class TestApiExposesTranslations:
    def test_news_prefers_english_and_ships_the_original(self, client, make_source):
        source = make_source("ukrainian", meta={"language": "Ukrainian"})
        item = NewsItem(
            source_id=source.id, content_hash=content_hash("Обстріл", "текст"),
            title="Обстріл Покровська", body="Російські війська атакували місто",
            title_en="Shelling of Pokrovsk", body_en="Russian forces attacked the city",
            llm_status="pending", translation_status="done",
        )
        db.session.add(item)
        db.session.commit()

        payload = client.get("/api/news").get_json()["items"][0]
        assert payload["title"] == "Shelling of Pokrovsk"
        assert payload["title_original"] == "Обстріл Покровська"
        assert payload["translated"] is True
        assert payload["snippet"].startswith("Russian forces")
        assert payload["snippet_original"].startswith("Російські")

    def test_untranslated_items_fall_back_to_the_original(self, client, make_source):
        source = make_source("western", meta={"language": "English"})
        db.session.add(
            NewsItem(
                source_id=source.id, content_hash=content_hash("English headline", "body"),
                title="English headline", body="body", llm_status="pending",
                translation_status="skipped",
            )
        )
        db.session.commit()

        payload = client.get("/api/news").get_json()["items"][0]
        assert payload["title"] == "English headline"
        assert payload["translated"] is False

    def test_map_events_carry_the_translated_title(self, client, make_source, make_event):
        source = make_source("russian", meta={"language": "Russian"})
        event = make_event(source, lat=48.5, lon=37.5, title="Удар по Покровську")
        db.session.execute(
            text("UPDATE news_items SET title_en = :t WHERE id = :i"),
            {"t": "Strike on Pokrovsk", "i": event.news_item_id},
        )
        db.session.commit()

        props = client.get("/api/events").get_json()["features"][0]["properties"]
        assert props["title"] == "Strike on Pokrovsk"
        assert props["title_original"] == "Удар по Покровську"
        assert props["translated"] is True


class TestSavedListSupport:
    """The shortlist lives in the browser; the server only has to make it addressable."""

    def test_events_expose_the_news_item_they_belong_to(self, client, make_source, make_event):
        event = make_event(make_source("western"), lat=48.5, lon=37.5)
        props = client.get("/api/events").get_json()["features"][0]["properties"]
        assert props["news_item_id"] == event.news_item_id

    def test_cluster_off_returns_points_even_past_the_threshold(self, client, app, make_source,
                                                                make_event, monkeypatch):
        """A shortlist cannot be filtered out of a cluster, so the client can opt out of them."""
        import dataclasses

        from app.config import settings

        monkeypatch.setattr(
            "app.api.events.settings",
            dataclasses.replace(settings, events_cluster_threshold=2),
        )
        source = make_source("russian")
        for i in range(5):
            make_event(source, lat=48.5 + i * 0.001, lon=37.5)

        clustered = client.get("/api/events?zoom=6").get_json()
        assert clustered["meta"]["clustered"] is True

        points = client.get("/api/events?zoom=6&cluster=off").get_json()
        assert points["meta"]["clustered"] is False
        assert len(points["features"]) == 5
        assert all("news_item_id" in f["properties"] for f in points["features"])
