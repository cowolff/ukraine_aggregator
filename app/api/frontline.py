from __future__ import annotations

from flask import request

from app.api import bad_request, bp, cached_json
from app.api.events import parse_iso
from app.config import settings
from app.services.frontline import latest_snapshots


@bp.get("/frontline")
def frontline():
    detail = (request.args.get("detail") or "mid").lower()
    if detail not in settings.snapshot_simplify_tolerances:
        return bad_request("detail must be one of low|mid|high")
    try:
        at = parse_iso(request.args.get("at"), "at")
    except ValueError as exc:
        return bad_request(str(exc))

    # Live responses are invalidation-cached (the builder drops api:frontline:* on write).
    # Historical ones never change, so they can be cached for much longer.
    ttl = None if at is None else 86400
    return cached_json(
        "frontline",
        {"detail": detail, "at": at.isoformat() if at else None},
        lambda: latest_snapshots(detail, at),
        ttl=ttl,
    )
