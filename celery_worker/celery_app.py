"""The single Celery instance for the whole system (PLAN §10).

The web tier imports it through ``app.extensions.get_celery`` so no second instance is ever
constructed — that duplication was the template's main smell.
"""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery = Celery(
    "ukraine",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "celery_worker.tasks.poll",
        "celery_worker.tasks.extract",
        "celery_worker.tasks.translate",
        "celery_worker.tasks.summarize",
        "celery_worker.tasks.synthesize",
        "celery_worker.tasks.dispatch",
        "celery_worker.tasks.geocode",
        "celery_worker.tasks.rules",
        "celery_worker.tasks.frontline",
        "celery_worker.tasks.maintenance",
    ],
)

celery.conf.update(
    task_default_queue="default",
    task_routes={
        "tasks.llm_extract_batch": {"queue": "llm"},
        "tasks.translate_batch": {"queue": "llm"},
        # The dispatchers are the control plane: they decide what everything else does, so they
        # get their own queue and can never queue behind a flood of poll tasks.
        "tasks.dispatch_llm": {"queue": "control"},
        "tasks.dispatch_polls": {"queue": "control"},
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=200,       # bound RSS growth on a 4 GB node
    result_expires=3600,
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    task_soft_time_limit=settings.celery_task_soft_time_limit,
    task_time_limit=settings.celery_task_time_limit,
    beat_schedule={
        "dispatch-polls": {"task": "tasks.dispatch_polls", "schedule": 60.0},
        # One dispatcher keeps the llm queue fed for both extraction and translation; firing a
        # single batch of each on a timer could not keep pace with ingest.
        "dispatch-llm": {"task": "tasks.dispatch_llm", "schedule": 30.0},
        "geocode-pending": {"task": "tasks.geocode_pending", "schedule": 120.0},
        "evaluate-claims": {"task": "tasks.evaluate_claims", "schedule": 300.0},
        # Offset from evaluate-claims so the two correlation passes do not always collide.
        "cluster-synthesis": {
            "task": "tasks.cluster_synthesis",
            "schedule": 300.0,
            "options": {"countdown": 90},
        },
        "rebuild-frontline": {"task": "tasks.rebuild_frontline", "schedule": 300.0},
        "rebuild-frontline-nightly": {
            "task": "tasks.rebuild_frontline",
            "schedule": crontab(hour=3, minute=30),
            "kwargs": {"force": True},
        },
        "pull-deepstate": {"task": "tasks.pull_deepstate", "schedule": 1800.0},
        "pull-isw": {"task": "tasks.pull_isw", "schedule": 3600.0},
        "pull-warspotting": {"task": "tasks.pull_warspotting", "schedule": 3600.0},
        "pull-geoconfirmed": {
            "task": "tasks.pull_geoconfirmed",
            "schedule": crontab(hour=4, minute=0),
        },
        "maintenance": {"task": "tasks.maintenance", "schedule": crontab(hour=2, minute=15)},
        # A dead worker's 'processing' claim must not wedge items until the nightly pass; the
        # staleness threshold (LLM_CLAIM_STALE_MINUTES) still protects live batches.
        "release-stale-claims": {"task": "tasks.release_stale_claims", "schedule": 600.0},
    },
)


def flask_app():
    """Lazily build a Flask app context for tasks that use the Flask-SQLAlchemy session."""
    from app import create_app

    return create_app()
