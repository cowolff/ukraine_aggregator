"""Append-only audit trail. Every mutation — admin or rule engine — lands here (PLAN §17)."""
from __future__ import annotations

from app.extensions import db, log


def audit(
    actor: str,
    action: str,
    *,
    entity: str | None = None,
    entity_id: str | None = None,
    detail: dict | None = None,
    commit: bool = False,
) -> None:
    from app.models import AuditLog

    entry = AuditLog(actor=actor, action=action, entity=entity, entity_id=entity_id, detail=detail or {})
    db.session.add(entry)
    if commit:
        db.session.commit()
    log.info("audit %s %s %s/%s", actor, action, entity, entity_id)


def current_actor() -> str:
    from flask import has_request_context
    from flask_login import current_user

    if has_request_context() and getattr(current_user, "is_authenticated", False):
        return f"admin:{current_user.username}"
    return "system"
