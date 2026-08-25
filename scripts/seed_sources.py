#!/usr/bin/env python
"""Seed the source catalogue from sources/ukraine_frontline_sources.csv (PLAN §9).

Idempotent: sources are upserted by URL. Only a curated working set is imported as pollable, and
everything starts disabled except the balanced starter set below.

    python scripts/seed_sources.py [--csv PATH] [--enable-starters/--no-enable-starters]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app import create_app
from app.extensions import db
from app.models import Source

DEFAULT_CSV = Path(__file__).resolve().parents[1] / "sources" / "ukraine_frontline_sources.csv"

PERSPECTIVE_MAP = {
    "Ukrainian": "ukrainian",
    "Russian": "russian",
    "Western": "western",
    "International/Neutral": "neutral",
}

INTERVAL_BY_FREQUENCY = {
    "Real-time": 300,
    "Multiple daily": 900,
    "Daily": 3600,
}
DEFAULT_INTERVAL = 7200

# Reliability tiers (1 = evidence-grade, 3 = never moves the map — see app/services/rules.py).
# Tier 1 is reserved for sources that verify rather than assert: independent OSINT, think tanks,
# and media with a formal verification operation. Governments — belligerent ministries and allied
# ones alike — are partisan primary sources: quotable as claims, so tier 2, never tier 1.
TIER1_SOURCE_TYPES = {"Think Tank/NGO", "Independent OSINT"}
# Phrases are deliberately specific: a note *praising* a source for labelling unverified claims
# (Meduza, CNN) must not trip the same pattern as one that *amplifies* unverified claims.
TIER3_NOTE_PATTERNS = re.compile(
    r"propagand|disinformation|low verification|state media|fabricat"
    r"|systematically unverified|amplif\w+ unverified|never as evidence"
    r"|wholly partisan",
    re.IGNORECASE,
)
# Notes that flag a pass-through/commentary source (aggregator re-renders, translation projects,
# advocacy think tanks): honest and usable, but they add no verification of their own — cap at 2.
TIER2_NOTE_PATTERNS = re.compile(r"no independent verification", re.IGNORECASE)
# Name-based overrides, first match wins. These beat the type/notes heuristics because the
# reliability of these outlets is established by track record, not by catalogue metadata.
TIER_OVERRIDES: list[tuple[re.Pattern, int]] = [
    # Belligerent-state propaganda organs: capture claims routinely premature, loss figures
    # routinely fabricated or inflated. Kept ingestible as a record of official claims.
    (re.compile(r"russian ministry of defen[cs]e|\btass\b|ria novosti|soloviev|readovka|zvezda", re.IGNORECASE), 3),
    # International outlets with formal verification desks (BBC Verify, Reuters/AFP fact-check
    # operations) and rigorous Russian exile media (Meduza, Mediazona named-death counts).
    (re.compile(r"\bbbc\b|reuters|agence france|deutsche welle|radio free europe|rfe/?rl|meduza|mediazona", re.IGNORECASE), 1),
    # State-founded analysis shops do messaging, not verification. StopFake and Necro Mancer are
    # rescued from note-pattern false positives: one *counters* disinformation, the other's
    # casualty-ID database is solid despite a note calling its tone propagandistic.
    (re.compile(r"russian international affairs council|stopfake|necro mancer", re.IGNORECASE), 2),
    # Satellite instrument data — the one government feed that measures instead of asserts.
    (re.compile(r"nasa firms", re.IGNORECASE), 1),
]
SKIP_NOTE_PATTERNS = re.compile(
    r"preview[s]? disabled|dormant|inactive|archived|no longer updat|defunct|dead\b",
    re.IGNORECASE,
)

FEED_URL_RE = re.compile(r"https?://[^\s;,]+")
TME_RE = re.compile(r"https?://t\.me/(?:s/)?([A-Za-z0-9_]{3,64})")

# The four §8 APIs, defined here rather than in the CSV so their adapters are explicit.
API_SOURCES = [
    {
        "name": "DeepStateMap (frontline polygons)",
        "type": "api",
        "url": "https://deepstatemap.live/api/history/last",
        "perspective": "ukrainian",
        "reliability_tier": 1,
        "poll_interval_s": 1800,
        "enabled": True,
        "meta": {"adapter": "deepstate"},
    },
    {
        "name": "ISW / Critical Threats assessed control",
        "type": "api",
        "url": "https://services5.arcgis.com/SaBe5HMtmnbqSWlu/arcgis/rest/services",
        "perspective": "western",
        "reliability_tier": 1,
        "poll_interval_s": 3600,
        "enabled": True,
        "meta": {"adapter": "isw", "service": "VIEW_RussiaCoTinUkraine_V3", "layer": 49},
    },
    {
        "name": "GeoConfirmed (verified geolocations)",
        "type": "api",
        "url": "https://geoconfirmed.org/api/Map/export/Ukraine/csv",
        "perspective": "neutral",
        "reliability_tier": 1,
        "poll_interval_s": 86400,
        "enabled": True,
        "meta": {"adapter": "geoconfirmed"},
    },
    {
        "name": "WarSpotting (geolocated equipment losses)",
        "type": "api",
        "url": "https://ukr.warspotting.net/api/losses/russia/recent/",
        "perspective": "neutral",
        "reliability_tier": 2,
        "poll_interval_s": 3600,
        "enabled": True,
        "meta": {"adapter": "warspotting"},
    },
]

# Starter set (~25 sources balanced across perspectives). Matched case-insensitively against the
# CSV `title`; the admin UI enables the rest.
STARTER_TITLES = [
    # Ukrainian
    "Ukrainian General Staff",
    "General Staff of the Armed Forces of Ukraine",
    "Ukrinform",
    "Ukrainska Pravda",
    "Suspilne",
    "Kyiv Independent",
    "DeepState",
    "Militarnyi",
    "Espreso",
    # Russian
    "Rybar",
    "WarGonzo",
    "Two Majors",
    "Readovka",
    "Russian Ministry of Defence",
    "Russian Ministry of Defense",
    "Andrei Marochko",
    # Western
    "Institute for the Study of War",
    "OSW",
    "Centre for Eastern Studies",
    "BBC",
    "Reuters",
    "Deutsche Welle",
    "Radio Free Europe",
    "Al Jazeera",
    # Neutral / OSINT
    "GeoConfirmed",
    "Clash Report",
    "War Mapper",
    "Frontelligence Insight",
    "Conflicts Tracker",
    "OSINT Warfare",
    "Monitor The Situation",
]


def perspective_of(row: dict) -> str | None:
    return PERSPECTIVE_MAP.get((row.get("affiliation_perspective") or "").strip())


def tier_of(row: dict) -> int:
    title = row.get("title") or ""
    for pattern, tier in TIER_OVERRIDES:
        if pattern.search(title):
            return tier
    notes = row.get("reliability_notes") or ""
    # Pass-through check first: a translation/aggregation project's note may warn that the
    # *upstream* content is propaganda (WarTranslated) without the project itself being tier 3.
    if TIER2_NOTE_PATTERNS.search(notes):
        return 2
    if TIER3_NOTE_PATTERNS.search(notes):
        return 3
    if (row.get("source_type") or "").strip() in TIER1_SOURCE_TYPES:
        return 1
    return 2


def interval_of(row: dict) -> int:
    return INTERVAL_BY_FREQUENCY.get((row.get("update_frequency") or "").strip(), DEFAULT_INTERVAL)


def rss_url_of(row: dict) -> str | None:
    """Pick a feed URL out of handle_or_feed / url, preferring something feed-shaped."""
    if "RSS" not in (row.get("machine_readable") or ""):
        return None
    candidates: list[str] = []
    for field in ("handle_or_feed", "url"):
        candidates.extend(FEED_URL_RE.findall(row.get(field) or ""))
    feedish = [c for c in candidates if re.search(r"(rss|feed|\.xml|atom)", c, re.IGNORECASE)]
    return (feedish or candidates or [None])[0]


def telegram_slug_of(row: dict) -> str | None:
    for field in ("url", "handle_or_feed"):
        match = TME_RE.search(row.get(field) or "")
        if match:
            return match.group(1)
    handle = (row.get("handle_or_feed") or "").strip()
    if (row.get("primary_platform") or "").strip() == "Telegram" and handle.startswith("@"):
        return handle[1:]
    return None


def is_starter(title: str) -> bool:
    lowered = title.lower()
    return any(name.lower() in lowered for name in STARTER_TITLES)


def rows_to_sources(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen_urls: set[str] = set()
    for row in rows:
        title = (row.get("title") or "").strip()
        perspective = perspective_of(row)
        if not title or not perspective:
            continue
        notes = row.get("reliability_notes") or ""
        access = (row.get("access") or "").lower()
        if "free" not in access:
            continue  # PLAN §21: only ingest sources catalogued as free

        platform = (row.get("primary_platform") or "").strip()
        slug = telegram_slug_of(row)
        if platform == "Telegram" and slug:
            if SKIP_NOTE_PATTERNS.search(notes):
                continue  # preview-disabled or dormant channels never parse
            url = f"https://t.me/s/{slug}"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            out.append(
                {
                    "name": title,
                    "type": "telegram",
                    "url": url,
                    "perspective": perspective,
                    "reliability_tier": tier_of(row),
                    "poll_interval_s": interval_of(row),
                    "enabled": is_starter(title),
                    "meta": {
                        "slug": slug,
                        "catalogue_category": row.get("category"),
                        # Used by the translator to skip feeds already published in English.
                        "language": row.get("language"),
                    },
                }
            )
            continue

        feed = rss_url_of(row)
        if feed and feed not in seen_urls:
            seen_urls.add(feed)
            out.append(
                {
                    "name": title,
                    "type": "rss",
                    "url": feed,
                    "perspective": perspective,
                    "reliability_tier": tier_of(row),
                    "poll_interval_s": interval_of(row),
                    "enabled": is_starter(title),
                    "meta": {
                        "catalogue_category": row.get("category"),
                        "language": row.get("language"),
                    },
                }
            )
    return out


def upsert(records: list[dict], enable_starters: bool) -> dict:
    created = updated = 0
    for record in records:
        existing = db.session.execute(
            select(Source).where(Source.url == record["url"])
        ).scalar_one_or_none()
        enabled = bool(record.get("enabled")) and enable_starters
        if existing is None:
            db.session.add(
                Source(
                    name=record["name"],
                    type=record["type"],
                    url=record["url"],
                    perspective=record["perspective"],
                    reliability_tier=record["reliability_tier"],
                    poll_interval_s=record["poll_interval_s"],
                    enabled=enabled,
                    meta=record.get("meta") or {},
                )
            )
            created += 1
        else:
            # Re-runs refresh catalogue-derived fields but never re-disable what an admin enabled.
            existing.name = record["name"]
            existing.type = record["type"]
            existing.perspective = record["perspective"]
            existing.reliability_tier = record["reliability_tier"]
            existing.poll_interval_s = record["poll_interval_s"]
            existing.meta = {**(existing.meta or {}), **(record.get("meta") or {})}
            if enabled:
                existing.enabled = True
            updated += 1
    db.session.commit()
    return {"created": created, "updated": updated}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--enable-starters", action="store_true", default=True)
    parser.add_argument("--no-enable-starters", dest="enable_starters", action="store_false")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"error: {args.csv} not found", file=sys.stderr)
        return 1

    with args.csv.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    catalogue = rows_to_sources(rows)
    app = create_app()
    with app.app_context():
        api_stats = upsert(API_SOURCES, True)
        stats = upsert(catalogue, args.enable_starters)
        totals = db.session.execute(
            db.text(
                "SELECT type, perspective, count(*) FILTER (WHERE enabled) AS on, count(*) AS all "
                "FROM sources GROUP BY type, perspective ORDER BY type, perspective"
            )
        ).all()
    print(f"catalogue rows: {len(rows)} → importable sources: {len(catalogue)}")
    print(f"apis: {api_stats}  catalogue: {stats}")
    for row in totals:
        print(f"  {row[0]:9s} {row[1]:10s} enabled={row[2]:3d} total={row[3]:3d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
