"""Queue dispatchers: keep the workers fed without letting one queue starve another."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models import NewsItem, content_hash


@pytest.fixture
def queues():
    """Track what each dispatcher enqueues, without a broker."""
    from app.extensions import redis_client

    for queue in ("default", "llm", "control"):
        try:
            redis_client.delete(queue)
        except Exception:
            pass
    return redis_client


class TestLLMDispatcher:
    def _items(self, source, count, *, llm_status="done", translation_status="pending"):
        for i in range(count):
            title = f"Обстріл {translation_status} {i}"
            db.session.add(
                NewsItem(
                    source_id=source.id,
                    content_hash=content_hash(title, f"{source.id}-{i}"),
                    title=title, body="текст", llm_status=llm_status,
                    translation_status=translation_status,
                )
            )
        db.session.commit()

    def test_fills_the_queue_up_to_the_target(self, make_source, queues, monkeypatch):
        from app.config import settings
        from celery_worker.tasks.dispatch import dispatch_llm

        sent = []
        monkeypatch.setattr("celery_worker.tasks.extract.llm_extract_batch.apply_async",
                            lambda **kw: sent.append("extract"))
        monkeypatch.setattr("celery_worker.tasks.translate.translate_batch.apply_async",
                            lambda **kw: sent.append("translate"))
        monkeypatch.setattr("celery_worker.tasks.summarize.summarize_batch.apply_async",
                            lambda **kw: sent.append("summarize"))

        self._items(make_source("ukrainian"), 40)
        result = dispatch_llm()
        assert result["queued"] == settings.llm_queue_target
        assert len(sent) == settings.llm_queue_target

    def test_does_not_pile_up_when_the_queue_is_already_full(self, make_source, queues, monkeypatch):
        from app.config import settings
        from celery_worker.tasks.dispatch import dispatch_llm

        sent = []
        monkeypatch.setattr("celery_worker.tasks.extract.llm_extract_batch.apply_async",
                            lambda **kw: sent.append("extract"))
        monkeypatch.setattr("celery_worker.tasks.translate.translate_batch.apply_async",
                            lambda **kw: sent.append("translate"))
        monkeypatch.setattr("celery_worker.tasks.summarize.summarize_batch.apply_async",
                            lambda **kw: sent.append("summarize"))
        monkeypatch.setattr("celery_worker.tasks.dispatch._queue_depth",
                            lambda: settings.llm_queue_target)

        self._items(make_source("ukrainian"), 40)
        assert dispatch_llm()["queued"] == 0
        assert sent == []

    def test_splits_capacity_between_extraction_and_translation(self, make_source, queues,
                                                                monkeypatch):
        from celery_worker.tasks.dispatch import dispatch_llm

        sent = []
        monkeypatch.setattr("celery_worker.tasks.extract.llm_extract_batch.apply_async",
                            lambda **kw: sent.append("extract"))
        monkeypatch.setattr("celery_worker.tasks.translate.translate_batch.apply_async",
                            lambda **kw: sent.append("translate"))
        monkeypatch.setattr("celery_worker.tasks.summarize.summarize_batch.apply_async",
                            lambda **kw: sent.append("summarize"))

        source = make_source("ukrainian")
        # Lots waiting to translate/summarise, a little waiting to extract. The two unextracted
        # items must not count toward the summary backlog — they are not claimable yet.
        self._items(source, 40, llm_status="done", translation_status="pending")
        self._items(source, 2, llm_status="pending", translation_status="skipped")

        result = dispatch_llm()
        assert sent.count("translate") > sent.count("extract"), "capacity follows the backlog"
        assert sent.count("extract") >= 1, "the smaller queue is never starved entirely"
        assert sent.count("summarize") >= 1, "extracted items are waiting on summaries"
        assert result["summarize_pending"] == 40, "unextracted items are not summary-claimable"

    def test_no_work_means_no_tasks(self, queues, monkeypatch):
        from celery_worker.tasks.dispatch import dispatch_llm

        sent = []
        monkeypatch.setattr("celery_worker.tasks.extract.llm_extract_batch.apply_async",
                            lambda **kw: sent.append("extract"))
        monkeypatch.setattr("celery_worker.tasks.translate.translate_batch.apply_async",
                            lambda **kw: sent.append("translate"))
        monkeypatch.setattr("celery_worker.tasks.summarize.summarize_batch.apply_async",
                            lambda **kw: sent.append("summarize"))
        assert dispatch_llm()["queued"] == 0
        assert sent == []


class TestPollFanOut:
    def test_backs_off_when_the_queue_is_already_deep(self, make_source, monkeypatch):
        """Unbounded fan-out built a 1,100-task backlog that starved every other task."""
        from app.config import settings
        from celery_worker.tasks.poll import dispatch_polls

        queued = []
        monkeypatch.setattr("celery_worker.tasks.poll.poll_source.apply_async",
                            lambda args=None, countdown=None: queued.append(args[0]))
        monkeypatch.setattr("celery_worker.tasks.poll._queue_depth",
                            lambda q: settings.poll_queue_max)

        for _ in range(5):
            make_source("western", type_="rss")
        result = dispatch_polls()
        assert result["dispatched"] == 0
        assert result["reason"] == "queue busy"
        assert queued == []

    def test_dispatches_only_up_to_the_remaining_headroom(self, make_source, monkeypatch):
        from app.config import settings
        from celery_worker.tasks.poll import dispatch_polls

        queued = []
        monkeypatch.setattr("celery_worker.tasks.poll.poll_source.apply_async",
                            lambda args=None, countdown=None: queued.append(args[0]))
        # Three slots left before the ceiling.
        monkeypatch.setattr("celery_worker.tasks.poll._queue_depth",
                            lambda q: settings.poll_queue_max - 3)

        for _ in range(10):
            make_source("western", type_="rss")
        result = dispatch_polls()
        assert result["dispatched"] == 3, "fan-out is capped by the headroom, not the source count"
        assert len(queued) == 3

    def test_dispatches_freely_when_the_queue_is_empty(self, make_source, monkeypatch):
        from celery_worker.tasks.poll import dispatch_polls

        queued = []
        monkeypatch.setattr("celery_worker.tasks.poll.poll_source.apply_async",
                            lambda args=None, countdown=None: queued.append(args[0]))
        monkeypatch.setattr("celery_worker.tasks.poll._queue_depth", lambda q: 0)

        for _ in range(6):
            make_source("western", type_="rss")
        assert dispatch_polls()["dispatched"] == 6
