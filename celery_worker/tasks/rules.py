"""Rule-engine tasks (PLAN §13)."""
from __future__ import annotations

from app.extensions import log
from app.services.rules import evaluate_claims as run_evaluation
from app.services.rules import intake_events as run_intake
from celery_worker.celery_app import celery
from celery_worker.context import with_app_context


@celery.task(name="tasks.evaluate_claims")
@with_app_context
def evaluate_claims() -> dict:
    intake = run_intake()
    evaluation = run_evaluation()
    log.info("claims intake=%s evaluation=%s", intake.as_dict(), evaluation.as_dict())
    return {"intake": intake.as_dict(), "evaluation": evaluation.as_dict()}
