"""Public read-only API (PLAN §15). Every response is orjson, ETagged and Redis-cached."""
from __future__ import annotations

from flask import Blueprint, Response, request

from app.config import settings
from app.services.cache import dumps, get_cached, params_key, set_cached

bp = Blueprint("api", __name__, url_prefix="/api")


def json_response(payload, *, status: int = 200, etag: str | None = None, max_age: int = 30) -> Response:
    body = dumps(payload) if not isinstance(payload, (bytes, bytearray)) else bytes(payload)
    resp = Response(body, status=status, mimetype="application/json")
    if etag:
        resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = f"public, max-age={max_age}"
    return resp


def cached_json(name: str, params: dict, builder, ttl: int | None):
    """Serve a cached JSON payload, honouring ``If-None-Match`` (PLAN §18)."""
    key = params_key(name, params)
    hit = get_cached(key)
    if hit is None:
        payload = builder()
        body = dumps(payload)
        etag = set_cached(key, body, ttl)
    else:
        etag, body = hit

    if request.headers.get("If-None-Match") == etag:
        resp = Response(status=304)
        resp.headers["ETag"] = etag
        return resp
    return json_response(body, etag=etag, max_age=min(ttl or 30, 120))


def bad_request(message: str):
    return json_response({"error": "bad_request", "detail": message}, status=400, max_age=0)


from app.api import events, frontline, meta, news, synthesis  # noqa: E402,F401  (register routes)
