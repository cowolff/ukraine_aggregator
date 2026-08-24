"""Redis response cache with ETag support (PLAN §18).

Key shape: ``api:<name>:<sha1 of sorted params>``. Values are stored as a two-field hash so a
conditional request can be answered from the ETag alone.
"""
from __future__ import annotations

import hashlib
from typing import Any

import orjson

from app.extensions import log, redis_client

DIRTY_KEY = "frontline:dirty"


def params_key(name: str, params: dict[str, Any]) -> str:
    flat = "&".join(f"{k}={params[k]}" for k in sorted(params) if params[k] is not None)
    return f"api:{name}:{hashlib.sha1(flat.encode()).hexdigest()[:16]}"


def etag_for(payload: bytes) -> str:
    return 'W/"' + hashlib.sha1(payload).hexdigest()[:20] + '"'


def get_cached(key: str) -> tuple[str, bytes] | None:
    try:
        data = redis_client.hgetall(key)
    except Exception as exc:  # Redis down must not take the API down
        log.warning("cache read failed: %s", exc)
        return None
    if not data:
        return None
    etag = data.get(b"etag")
    body = data.get(b"body")
    if not etag or body is None:
        return None
    return etag.decode(), body


def set_cached(key: str, body: bytes, ttl: int | None) -> str:
    etag = etag_for(body)
    try:
        pipe = redis_client.pipeline()
        pipe.hset(key, mapping={"etag": etag, "body": body})
        if ttl:
            pipe.expire(key, ttl)
        pipe.execute()
    except Exception as exc:
        log.warning("cache write failed: %s", exc)
    return etag


def invalidate(prefix: str) -> int:
    """Delete every cache key under a prefix. SCAN-based: never blocks Redis."""
    removed = 0
    try:
        for key in redis_client.scan_iter(match=f"{prefix}*", count=200):
            redis_client.delete(key)
            removed += 1
    except Exception as exc:
        log.warning("cache invalidate failed: %s", exc)
    return removed


def dumps(obj: Any) -> bytes:
    return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS)


def mark_frontline_dirty() -> None:
    try:
        redis_client.set(DIRTY_KEY, 1)
    except Exception as exc:
        log.warning("could not set dirty flag: %s", exc)


def frontline_is_dirty() -> bool:
    try:
        return bool(redis_client.get(DIRTY_KEY))
    except Exception:
        return True  # fail towards rebuilding rather than serving stale geometry


def clear_frontline_dirty() -> None:
    try:
        redis_client.delete(DIRTY_KEY)
    except Exception as exc:
        log.warning("could not clear dirty flag: %s", exc)


def bump_stat(name: str, amount: int = 1) -> None:
    """Metrics-lite counters surfaced on the admin dashboard (PLAN §18)."""
    try:
        redis_client.incrby(f"stats:{name}", amount)
    except Exception:
        pass


def read_stats(names: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        values = redis_client.mget([f"stats:{n}" for n in names])
    except Exception:
        return {n: 0 for n in names}
    for name, raw in zip(names, values):
        out[name] = int(raw) if raw else 0
    return out
