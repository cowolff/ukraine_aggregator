"""Polling: dedupe, conditional GET, failure accounting, markup-drift detection (PLAN §8.6, §10)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import text

from app.extensions import db
from app.models import NewsItem, Source, content_hash
from celery_worker.adapters import rss, telegram_web
from celery_worker.adapters.http import FetchError, Forbidden
from celery_worker.tasks.poll import DEAD_AT, DEGRADED_AT, dispatch_polls, ingest_items, poll_source


def items(n=2, prefix="item"):
    return [
        {
            "external_id": f"ext-{i}",
            "title": f"{prefix} {i}",
            "body": f"body of {prefix} {i}",
            "url": f"https://example.test/{prefix}/{i}",
            "published_at": dt.datetime.now(dt.timezone.utc),
        }
        for i in range(n)
    ]


def count_news() -> int:
    return int(db.session.execute(text("SELECT count(*) FROM news_items")).scalar())


class TestDedupe:
    def test_content_hash_is_normalisation_insensitive(self):
        assert content_hash("Hit  near\n Pokrovsk", "b") == content_hash("hit near pokrovsk", "b")
        assert content_hash("a", "b") != content_hash("a", "c")

    def test_second_ingest_of_the_same_items_inserts_nothing(self, make_source):
        source = make_source("western")
        batch = items(3)
        assert ingest_items(source, batch) == 3
        db.session.commit()
        assert ingest_items(source, batch) == 0
        db.session.commit()
        assert count_news() == 3

    def test_partially_overlapping_batch_inserts_only_the_new(self, make_source):
        source = make_source("western")
        ingest_items(source, items(2))
        db.session.commit()
        assert ingest_items(source, items(4)) == 2
        db.session.commit()
        assert count_news() == 4

    def test_identical_text_from_two_sources_is_deduped_globally(self, make_source):
        """Aggregators repost each other verbatim; one story should appear once."""
        first, second = make_source("russian"), make_source("neutral")
        assert ingest_items(first, items(1)) == 1
        db.session.commit()
        assert ingest_items(second, items(1)) == 0
        db.session.commit()

    def test_empty_items_are_ignored(self, make_source):
        source = make_source("western")
        assert ingest_items(source, [{"title": "", "body": "  "}, {"title": None, "body": None}]) == 0

    def test_items_land_as_pending_for_extraction(self, make_source):
        source = make_source("western")
        ingest_items(source, items(1))
        db.session.commit()
        assert db.session.execute(text("SELECT llm_status FROM news_items")).scalar() == "pending"


class TestPollSource:
    def test_successful_poll_ingests_and_marks_ok(self, make_source, monkeypatch):
        source = make_source("western", type_="rss")
        monkeypatch.setattr(rss, "poll", lambda s: (items(2), {"etag": 'W/"x"', "last_modified": None}))
        result = poll_source(source.id)
        assert result["inserted"] == 2

        db.session.expire_all()
        fresh = db.session.get(Source, source.id)
        assert fresh.status == "ok"
        assert fresh.etag == 'W/"x"'
        assert fresh.last_success_at is not None

    def test_304_not_modified_is_a_success_with_no_inserts(self, make_source, monkeypatch):
        source = make_source("western", type_="rss")
        monkeypatch.setattr(rss, "poll", lambda s: ([], {"not_modified": True}))
        result = poll_source(source.id)
        assert result["not_modified"] is True
        assert count_news() == 0
        db.session.expire_all()
        assert db.session.get(Source, source.id).last_success_at is not None

    def test_conditional_headers_are_sent_from_stored_state(self, make_source, monkeypatch):
        source = make_source("western", type_="rss")
        source.etag = 'W/"abc"'
        source.last_modified = "Wed, 20 Aug 2026 12:00:00 GMT"
        db.session.commit()
        seen = {}

        def fake_fetch(url, *, etag=None, last_modified=None):
            seen["etag"] = etag
            seen["last_modified"] = last_modified
            return rss.Fetched(304, b"", etag, last_modified, not_modified=True) \
                if hasattr(rss, "Fetched") else _stub_304(etag, last_modified)

        def _stub_304(etag, last_modified):
            from celery_worker.adapters.http import Fetched

            return Fetched(304, b"", etag, last_modified, not_modified=True)

        monkeypatch.setattr("celery_worker.adapters.rss.fetch", fake_fetch)
        poll_source(source.id)
        assert seen["etag"] == 'W/"abc"'
        assert seen["last_modified"] == "Wed, 20 Aug 2026 12:00:00 GMT"

    def test_403_degrades_but_never_kills(self, make_source, monkeypatch):
        source = make_source("western", type_="rss")
        monkeypatch.setattr(rss, "poll", lambda s: (_ for _ in ()).throw(Forbidden("403")))
        for _ in range(DEAD_AT + 5):
            poll_source(source.id)
        db.session.expire_all()
        fresh = db.session.get(Source, source.id)
        assert fresh.status == "degraded", "a datacenter-IP block must not mark a source dead"
        assert fresh.consecutive_failures > DEAD_AT

    def test_repeated_failures_escalate_to_degraded_then_dead(self, make_source, monkeypatch):
        source = make_source("western", type_="rss")
        monkeypatch.setattr(rss, "poll", lambda s: (_ for _ in ()).throw(FetchError("boom")))

        for _ in range(DEGRADED_AT):
            poll_source(source.id)
        db.session.expire_all()
        assert db.session.get(Source, source.id).status == "degraded"

        for _ in range(DEAD_AT - DEGRADED_AT):
            poll_source(source.id)
        db.session.expire_all()
        assert db.session.get(Source, source.id).status == "dead"

    def test_dead_sources_are_skipped(self, make_source, monkeypatch):
        source = make_source("western", type_="rss")
        source.status = "dead"
        db.session.commit()
        called = {"n": 0}

        def counting(_s):
            called["n"] += 1
            return [], {}

        monkeypatch.setattr(rss, "poll", counting)
        assert poll_source(source.id)["skipped"] is True
        assert called["n"] == 0

    def test_success_resets_the_failure_counter(self, make_source, monkeypatch):
        source = make_source("western", type_="rss")
        source.consecutive_failures = 4
        source.status = "degraded"
        db.session.commit()
        monkeypatch.setattr(rss, "poll", lambda s: (items(1), {"etag": None, "last_modified": None}))
        poll_source(source.id)
        db.session.expire_all()
        fresh = db.session.get(Source, source.id)
        assert fresh.consecutive_failures == 0 and fresh.status == "ok"


class TestTelegramDrift:
    def test_repeated_empty_pages_mark_the_source_degraded(self, make_source, monkeypatch):
        source = make_source("russian", type_="telegram", meta={"slug": "somechannel"})
        monkeypatch.setattr(telegram_web, "poll", lambda s: ([], {"empty_page": True}))
        for _ in range(5):
            poll_source(source.id)
        db.session.expire_all()
        fresh = db.session.get(Source, source.id)
        assert fresh.status == "degraded"
        assert fresh.meta["empty_pages"] >= 5

    def test_a_quiet_channel_is_not_treated_as_drift(self, make_source, monkeypatch):
        """A channel that parsed fine but posted nothing new must stay healthy.

        Conflating "no new messages" with "page parsed to nothing" degraded every low-traffic
        channel over time.
        """
        source = make_source("russian", type_="telegram", meta={"slug": "quiet"})
        monkeypatch.setattr(
            telegram_web, "poll",
            lambda s: ([], {"parsed_total": 17, "empty_page": False}),
        )
        for _ in range(8):
            poll_source(source.id)
        db.session.expire_all()
        fresh = db.session.get(Source, source.id)
        assert fresh.status == "ok"
        assert "empty_pages" not in (fresh.meta or {})

    def test_last_external_id_is_remembered(self, make_source, monkeypatch):
        source = make_source("russian", type_="telegram", meta={"slug": "somechannel"})
        monkeypatch.setattr(
            telegram_web, "poll",
            lambda s: (items(1), {"meta_update": {"last_external_id": "somechannel/42"}}),
        )
        poll_source(source.id)
        db.session.expire_all()
        assert db.session.get(Source, source.id).meta["last_external_id"] == "somechannel/42"

    def test_parsed_total_counts_before_the_newer_than_filter(self):
        html = """
        <div class="tgme_widget_message" data-post="chan/10">
          <div class="tgme_widget_message_text">old message</div>
          <div class="tgme_widget_message_date"><time datetime="2026-08-01T10:00:00+00:00"></time></div>
        </div>"""

        class FakeSource:
            id = 1
            url = "https://t.me/s/chan"
            meta = {"slug": "chan", "last_external_id": "chan/10"}

        from celery_worker.adapters.http import Fetched

        import celery_worker.adapters.telegram_web as tg

        original = tg.fetch
        tg.fetch = lambda url, **kw: Fetched(200, html.encode())
        try:
            parsed, state = tg.poll(FakeSource())
        finally:
            tg.fetch = original
        assert parsed == [], "nothing newer than chan/10"
        assert state["parsed_total"] == 1, "the page did parse one message"
        assert state["empty_page"] is False, "a quiet channel is not markup drift"

    def test_only_messages_newer_than_the_stored_id_are_kept(self):
        html = """
        <div class="tgme_widget_message" data-post="chan/10">
          <div class="tgme_widget_message_text">old message</div>
          <div class="tgme_widget_message_date"><time datetime="2026-08-01T10:00:00+00:00"></time></div>
        </div>
        <div class="tgme_widget_message" data-post="chan/11">
          <div class="tgme_widget_message_text">new message</div>
          <div class="tgme_widget_message_date"><time datetime="2026-08-02T10:00:00+00:00"></time></div>
        </div>"""

        class FakeSource:
            id = 1
            url = "https://t.me/s/chan"
            meta = {"slug": "chan", "last_external_id": "chan/10"}

        from celery_worker.adapters.http import Fetched

        import celery_worker.adapters.telegram_web as tg

        original = tg.fetch
        tg.fetch = lambda url, **kw: Fetched(200, html.encode())
        try:
            parsed, state = tg.poll(FakeSource())
        finally:
            tg.fetch = original
        assert [i["external_id"] for i in parsed] == ["chan/11"]
        assert state["meta_update"]["last_external_id"] == "chan/11"


class TestDispatch:
    def test_only_due_enabled_pollable_sources_are_dispatched(self, make_source, monkeypatch):
        due = make_source("western", type_="rss")
        disabled = make_source("western", type_="rss", enabled=False)
        dead = make_source("western", type_="rss")
        dead.status = "dead"
        recent = make_source("western", type_="rss")
        recent.last_polled_at = dt.datetime.now(dt.timezone.utc)
        api = make_source("neutral", type_="api", meta={"adapter": "deepstate"})
        db.session.commit()

        queued = []
        monkeypatch.setattr(
            "celery_worker.tasks.poll.poll_source.apply_async",
            lambda args=None, countdown=None: queued.append(args[0]),
        )
        result = dispatch_polls()
        assert result["dispatched"] == 1
        assert queued == [due.id]
        for source in (disabled, dead, recent, api):
            assert source.id not in queued
