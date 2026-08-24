"""RSS/Atom adapter with conditional GET (PLAN §8.6)."""
from __future__ import annotations

import datetime as dt
import re

import feedparser

from celery_worker.adapters.http import fetch

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n\s*\n+")


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text_body = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
    text_body = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text_body)
    text_body = _TAGS.sub("", text_body)
    text_body = (
        text_body.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return _WS.sub("\n\n", text_body).strip()


def _published(entry) -> dt.datetime | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def parse_feed(payload: bytes) -> list[dict]:
    feed = feedparser.parse(payload)
    items = []
    for entry in feed.entries:
        body = ""
        if entry.get("content"):
            body = " ".join(c.get("value", "") for c in entry["content"])
        body = body or entry.get("summary") or entry.get("description") or ""
        items.append(
            {
                "external_id": entry.get("id") or entry.get("guid") or entry.get("link"),
                "title": strip_html(entry.get("title")),
                "body": strip_html(body),
                "url": entry.get("link"),
                "published_at": _published(entry),
            }
        )
    return items


def poll(source) -> tuple[list[dict], dict]:
    """Returns (items, state) where state carries etag/last_modified to persist."""
    result = fetch(source.url, etag=source.etag, last_modified=source.last_modified)
    if result.not_modified:
        return [], {"not_modified": True}
    items = parse_feed(result.body)
    return items, {
        "etag": result.etag,
        "last_modified": result.last_modified,
        "not_modified": False,
    }
