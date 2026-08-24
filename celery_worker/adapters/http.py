"""Shared outbound HTTP: one client config, conditional GETs, gzip, polite UA (PLAN §18)."""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings


class FetchError(RuntimeError):
    """Permanent-ish failure: counts towards consecutive_failures."""


class Forbidden(FetchError):
    """403 — several catalogued sites block datacenter IPs. Degraded, never dead (PLAN §8.6)."""


@dataclass
class Fetched:
    status: int
    body: bytes
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def client(**kwargs) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(settings.http_timeout_s, connect=10.0),
        headers={
            "User-Agent": settings.http_user_agent,
            "Accept-Encoding": "gzip, deflate",
        },
        follow_redirects=True,
        **kwargs,
    )


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=1, max=12),
    reraise=True,
)
def _get(url: str, headers: dict) -> httpx.Response:
    with client() as http:
        resp = http.get(url, headers=headers)
    if resp.status_code >= 500:
        resp.raise_for_status()  # retried by tenacity
    return resp


def fetch(url: str, *, etag: str | None = None, last_modified: str | None = None) -> Fetched:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        resp = _get(url, headers)
    except httpx.HTTPStatusError as exc:
        raise FetchError(f"{url}: HTTP {exc.response.status_code}") from exc
    except httpx.TransportError as exc:
        raise FetchError(f"{url}: {type(exc).__name__}: {exc}") from exc

    if resp.status_code == 304:
        return Fetched(304, b"", etag, last_modified, not_modified=True)
    if resp.status_code == 403:
        raise Forbidden(f"{url}: HTTP 403 (datacenter IP block?)")
    if resp.status_code >= 400:
        raise FetchError(f"{url}: HTTP {resp.status_code}")
    return Fetched(
        resp.status_code,
        resp.content,
        resp.headers.get("ETag"),
        resp.headers.get("Last-Modified"),
    )


def fetch_json(url: str) -> object:
    import orjson

    return orjson.loads(fetch(url).body)


def stream_to_file(url: str, path: str) -> int:
    """Stream a large export (GeoConfirmed CSV is ~31 MB) to disk instead of into RAM."""
    total = 0
    with client() as http, http.stream("GET", url) as resp:
        if resp.status_code >= 400:
            raise FetchError(f"{url}: HTTP {resp.status_code}")
        with open(path, "wb") as handle:
            for chunk in resp.iter_bytes(chunk_size=262_144):
                handle.write(chunk)
                total += len(chunk)
    return total
