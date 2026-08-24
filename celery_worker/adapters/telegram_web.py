"""Telegram public-channel adapter: scrapes ``t.me/s/<slug>`` (PLAN §8.6).

Markup drift is a first-class failure mode: a 200 page yielding zero messages is reported so the
poller can mark the source degraded rather than crash.
"""
from __future__ import annotations

import datetime as dt
import re

from bs4 import BeautifulSoup

from celery_worker.adapters.http import fetch

SLUG_RE = re.compile(r"t\.me/(?:s/)?(@?)([A-Za-z0-9_]{3,64})")

# A line has to contain a letter or digit to be a headline. Telegram posts routinely open with a
# decorative emoji line ("⚡️", "📝", "🟠"), and taking the first line verbatim put those in the
# feed and in map popups as the headline.
_HAS_WORD = re.compile(r"[^\W_]", re.UNICODE)
TITLE_MIN_CHARS = 24
TITLE_MAX_CHARS = 200


def headline(body: str) -> str:
    """First substantive line of a post, joined with the next while it is too short to stand alone."""
    lines = [line.strip() for line in (body or "").split("\n")]
    meaningful = [line for line in lines if line and _HAS_WORD.search(line)]
    if not meaningful:
        return (body or "").strip()[:TITLE_MAX_CHARS]

    title = meaningful[0]
    for extra in meaningful[1:]:
        if len(title) >= TITLE_MIN_CHARS:
            break
        title = f"{title} {extra}".strip()
    return title[:TITLE_MAX_CHARS]


def slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = SLUG_RE.search(url)
    return match.group(2) if match else None


def channel_url(source) -> str:
    slug = (source.meta or {}).get("slug") or slug_from_url(source.url)
    if not slug:
        raise ValueError(f"source {source.id}: no telegram slug in meta or url")
    return f"https://t.me/s/{slug}"


def parse_page(html: str, slug: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items: list[dict] = []
    for block in soup.select(".tgme_widget_message"):
        post = block.get("data-post") or ""
        text_node = block.select_one(".tgme_widget_message_text")
        body = text_node.get_text("\n", strip=True) if text_node else ""
        time_node = block.select_one(".tgme_widget_message_date time[datetime]")
        published = None
        if time_node and time_node.get("datetime"):
            try:
                published = dt.datetime.fromisoformat(time_node["datetime"].replace("Z", "+00:00"))
            except ValueError:
                published = None
        if not body:
            # Media-only post: keep it only if it carries a caption elsewhere, else skip.
            continue
        items.append(
            {
                "external_id": post or None,
                "title": headline(body),
                "body": body,
                "url": f"https://t.me/{post}" if post else None,
                "published_at": published,
            }
        )
    return items


def poll(source) -> tuple[list[dict], dict]:
    url = channel_url(source)
    result = fetch(url)
    slug = (source.meta or {}).get("slug") or slug_from_url(source.url)
    items = parse_page(result.text, slug)
    # Markup drift is "the page yielded no messages at all", which must be measured *before* the
    # newer-than filter. Conflating the two marked every healthy low-traffic channel as degraded,
    # because a channel that simply has not posted since the last poll returns zero new items.
    parsed_total = len(items)

    # Keep only messages newer than the last stored external_id (post ids are monotonic).
    last_id = (source.meta or {}).get("last_external_id")
    if last_id:
        last_num = _post_number(last_id)
        if last_num is not None:
            items = [i for i in items if (_post_number(i["external_id"]) or 0) > last_num]

    state: dict = {
        "not_modified": False,
        "parsed_total": parsed_total,
        "empty_page": parsed_total == 0 and bool(result.body),
    }
    if items:
        newest = max(items, key=lambda i: _post_number(i["external_id"]) or 0)
        state["meta_update"] = {"last_external_id": newest["external_id"]}
    return items, state


def _post_number(external_id: str | None) -> int | None:
    if not external_id or "/" not in external_id:
        return None
    tail = external_id.rsplit("/", 1)[1]
    return int(tail) if tail.isdigit() else None
