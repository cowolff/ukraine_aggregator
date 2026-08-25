#!/usr/bin/env python
"""Build the local gazetteer (PLAN §8.5). Idempotent; run once at setup.

    python scripts/seed_gazetteer.py                     # GeoNames (default)
    python scripts/seed_gazetteer.py --source overpass    # OSM via Overpass, chunked by oblast
    python scripts/seed_gazetteer.py --katottg FILE.csv   # also join KATOTTG admin codes
    python scripts/seed_gazetteer.py --fixture            # tiny offline set, for tests/CI

**Why GeoNames is the default** (a documented deviation from §8.5, which specifies KATOTTG + an
Overpass join — see plans/DEVIATIONS.md): the Overpass path needs 27 separate area queries and
proved unusable in practice — the main endpoint refused connections from the build host and two
independent mirrors returned HTTP 500 for individual oblasts, leaving 10 of 27 loaded and only
~10k settlements. GeoNames ships the same information as one 2 MB download with no rate limit:
33,700+ populated places with coordinates, population, oblast codes and — importantly for §12 —
Ukrainian, Russian and Latin name variants already in an ``alternatenames`` column.

Overpass remains available via ``--source overpass`` (it carries settlement polygons GeoNames
lacks), and ``--katottg`` still attaches the authoritative admin codes when the codifier export
is supplied. Upserts by katottg when present, else by (name_uk, oblast, proximity).
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import time
import zipfile
from pathlib import Path

import orjson

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.services.geocode import canonical_oblast, name_search_value, normalize

CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache"
OVERPASS_CACHE = CACHE_DIR / "overpass"
FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "gazetteer_fixture.json"

GEONAMES_URL = "https://download.geonames.org/export/dump/UA.zip"
GEONAMES_ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"

# GeoNames feature codes worth mapping. Abandoned/historic codes are kept out: they duplicate
# or misplace real settlements.
# Alternate-name selection: how close a variant's transliteration must be to the ASCII name, and
# how many to keep per settlement.
VARIANT_MIN_RATIO = 0.5
VARIANT_LIMIT = 20

PLACE_CODES = {
    "PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLA5", "PPLC", "PPLG", "PPLS", "PPLL", "PPLF",
    # PPLX — a named section of a city (Saltivka, Troieshchyna, Pozniaky). Strike reports name
    # these constantly; without them the matcher either drops the event or, worse, snaps it onto
    # a like-named village elsewhere. They ship with population 0, so the population tie-break
    # still prefers a real settlement of the same name.
    "PPLX",
}

# GeoNames admin1 regions that ARE cities: their ADM2 rows are the official city districts
# ("Holosiiv Raion", pop 202,993) rather than oblast raions, and news cite them daily
# ("a 16-story building in Holosiivskyi district"). Other oblasts' ADM2 rows stay out —
# raion-level centroids are too coarse to pin an event to.
CITY_REGION_ADMIN1 = {"12", "20"}  # Kyiv City, Sevastopol City

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

OBLASTS: dict[str, str] = {
    "UA-05": "vinnytsia", "UA-07": "volyn", "UA-09": "luhansk", "UA-12": "dnipropetrovsk",
    "UA-14": "donetsk", "UA-18": "zhytomyr", "UA-21": "zakarpattia", "UA-23": "zaporizhzhia",
    "UA-26": "ivano-frankivsk", "UA-30": "kyiv", "UA-32": "kyiv", "UA-35": "kirovohrad",
    "UA-40": "sevastopol", "UA-43": "crimea", "UA-46": "lviv", "UA-48": "mykolaiv",
    "UA-51": "odesa", "UA-53": "poltava", "UA-56": "rivne", "UA-59": "sumy",
    "UA-61": "ternopil", "UA-63": "kharkiv", "UA-65": "kherson", "UA-68": "khmelnytskyi",
    "UA-71": "cherkasy", "UA-74": "chernihiv", "UA-77": "chernivtsi",
}
PLACE_TYPES = "city|town|village|hamlet"
QUERY = """
[out:json][timeout:300];
area["ISO3166-2"="{iso}"]->.searchArea;
(
  node["place"~"^({places})$"](area.searchArea);
);
out tags center;
"""

GEONAMES_FIELDS = (
    "geonameid name asciiname alternatenames lat lon fclass fcode country cc2 "
    "admin1 admin2 admin3 admin4 population elevation dem tz modified"
).split()


def _http_get(url: str, *, timeout: float = 180.0) -> bytes:
    import httpx

    from app.config import settings

    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=15.0),
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    ) as client:
        resp = client.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"{url}: HTTP {resp.status_code}")
    return resp.content


def _cached(name: str, url: str, *, force: bool = False) -> bytes:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    if path.exists() and not force:
        return path.read_bytes()
    data = _http_get(url)
    path.write_bytes(data)
    return data


# --------------------------------------------------------------------------------------------
# GeoNames
# --------------------------------------------------------------------------------------------
def admin1_map(force: bool = False) -> dict[str, str]:
    """``UA.05`` → canonical oblast key."""
    raw = _cached("geonames_admin1.txt", GEONAMES_ADMIN1_URL, force=force).decode("utf-8")
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.startswith("UA."):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        canon = canonical_oblast(parts[2])
        if canon:
            out[parts[0]] = canon
    return out


def _is_cyrillic(value: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in value)


def _split_alternates(value: str, ascii_name: str) -> tuple[str | None, str | None, list[str]]:
    """Choose Ukrainian and Russian name forms out of GeoNames' alternatenames column.

    The plain UA dump carries no language tags, and Bulgarian, Serbian and Macedonian exonyms are
    Cyrillic too — taking the *first* Cyrillic alternate picked "Лавов" for Lviv. Instead each
    Cyrillic candidate is transliterated and ranked by closeness to the ASCII name, which reliably
    surfaces the Ukrainian form ("Львів" → lviv, an exact hit) and leaves the runner-up as the
    Russian one. Every variant is still returned for indexing, so matching never depends on this
    choice being perfect.
    """
    from difflib import SequenceMatcher

    target = normalize(ascii_name)
    cyrillic: list[tuple[float, str]] = []
    scored: list[tuple[float, str]] = []
    for candidate in (value or "").split(","):
        candidate = candidate.strip()
        if not candidate or len(candidate) > 80:
            continue
        score = SequenceMatcher(None, normalize(candidate), target).ratio()
        scored.append((score, candidate))
        if _is_cyrillic(candidate):
            cyrillic.append((score, candidate))

    cyrillic.sort(key=lambda pair: (-pair[0], pair[1]))
    name_uk = cyrillic[0][1] if cyrillic else None
    name_ru = cyrillic[1][1] if len(cyrillic) > 1 else None

    # Well-known cities carry hundreds of alternates, so the list has to be trimmed — but taking
    # the *first* N drops the forms that matter: Kyiv's Russian "Киев" fell off the end and
    # reports spelling it that way then resolved to a like-named village. Rank by closeness to the
    # ASCII name instead, which keeps the real transliterations and sheds distant exonyms.
    scored.sort(key=lambda pair: (-pair[0], len(pair[1])))
    variants = [name for score, name in scored if score >= VARIANT_MIN_RATIO][:VARIANT_LIMIT]
    return name_uk, name_ru, variants


def geonames_places(force: bool = False) -> list[dict]:
    payload = _cached("geonames_UA.zip", GEONAMES_URL, force=force)
    admin1 = admin1_map(force=force)
    archive = zipfile.ZipFile(io.BytesIO(payload))
    raw = archive.read("UA.txt").decode("utf-8")

    places: list[dict] = []
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) < len(GEONAMES_FIELDS):
            continue
        row = dict(zip(GEONAMES_FIELDS, fields))
        is_settlement = row["fclass"] == "P" and row["fcode"] in PLACE_CODES
        is_city_district = (
            row["fclass"] == "A" and row["fcode"] == "ADM2" and row["admin1"] in CITY_REGION_ADMIN1
        )
        if not (is_settlement or is_city_district):
            continue
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except ValueError:
            continue
        cyrillic, russian, variants = _split_alternates(row["alternatenames"], row["asciiname"])
        try:
            population = int(row["population"] or 0) or None
        except ValueError:
            population = None
        places.append(
            {
                "name_uk": (cyrillic or row["name"]).strip(),
                "name_ru": russian,
                "name_en": (row["asciiname"] or row["name"] or "").strip() or None,
                "oblast": admin1.get(f"UA.{row['admin1']}"),
                "raion": None,
                "lat": lat,
                "lon": lon,
                "population": population,
                # Every variant is indexed into name_search so a match never depends on which
                # form was chosen for display.
                "extra_names": [row["name"], row["asciiname"], *variants],
            }
        )
    return places


# --------------------------------------------------------------------------------------------
# Overpass (optional)
# --------------------------------------------------------------------------------------------
def overpass(iso: str, *, force: bool = False) -> dict:
    OVERPASS_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = OVERPASS_CACHE / f"{iso}.json"
    if cache_file.exists() and not force:
        return orjson.loads(cache_file.read_bytes())

    import httpx

    from app.config import settings

    query = QUERY.format(iso=iso, places=PLACE_TYPES)
    last_error: Exception | None = None
    backoffs = (30, 60, 120)
    for endpoint in OVERPASS_ENDPOINTS:
        for wait in backoffs:
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(300.0, connect=15.0),
                    headers={"User-Agent": settings.http_user_agent},
                ) as client:
                    resp = client.post(endpoint, data={"data": query})
                if resp.status_code == 200:
                    cache_file.write_bytes(resp.content)
                    return resp.json()
                last_error = RuntimeError(f"{endpoint}: HTTP {resp.status_code}")
                if resp.status_code == 429 or resp.status_code >= 500:
                    print(f"    {iso}: {endpoint} HTTP {resp.status_code}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                break
            except Exception as exc:
                last_error = exc
                print(f"    {iso}: {type(exc).__name__}, waiting {wait}s")
                time.sleep(wait)
    raise RuntimeError(f"overpass failed for {iso}: {last_error}")


def elements_to_places(payload: dict, oblast: str) -> list[dict]:
    out = []
    for element in payload.get("elements", []):
        tags = element.get("tags") or {}
        name_uk = tags.get("name:uk") or tags.get("name")
        if not name_uk:
            continue
        lat = element.get("lat") or (element.get("center") or {}).get("lat")
        lon = element.get("lon") or (element.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        try:
            population = int(str(tags.get("population", "")).replace(" ", "")) or None
        except ValueError:
            population = None
        out.append(
            {
                "name_uk": name_uk.strip(),
                "name_ru": (tags.get("name:ru") or "").strip() or None,
                "name_en": (tags.get("name:en") or "").strip() or None,
                "oblast": oblast,
                "raion": (tags.get("addr:district") or "").strip() or None,
                "lat": float(lat),
                "lon": float(lon),
                "population": population,
                "extra_names": [tags.get("name"), tags.get("name:en"), tags.get("name:ru")],
            }
        )
    return out


def collect_overpass(only: str | None, force: bool) -> list[dict]:
    codes = [c.strip() for c in only.split(",")] if only else list(OBLASTS.keys())
    places: list[dict] = []
    for iso in codes:
        oblast = OBLASTS.get(iso, iso.lower())
        try:
            payload = overpass(iso, force=force)
        except RuntimeError as exc:
            print(f"  ! {iso} ({oblast}): {exc}", file=sys.stderr)
            continue
        chunk = elements_to_places(payload, oblast)
        places.extend(chunk)
        print(f"  {iso} {oblast:16s} {len(chunk):6d} places", flush=True)
        time.sleep(2)
    return places


# --------------------------------------------------------------------------------------------
# KATOTTG + load
# --------------------------------------------------------------------------------------------
def load_katottg(path: Path) -> dict[tuple[str, str], str]:
    mapping: dict[tuple[str, str], str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        for row in csv.DictReader(handle, delimiter=delimiter):
            values = [v for v in row.values() if v]
            if not values:
                continue
            name = (values[-1] or "").strip()
            code = next((v for v in values if v and v.startswith("UA")), None)
            if not name or not code:
                continue
            oblast_cell = next((v for v in values if v and "област" in v.lower()), "")
            canon = canonical_oblast(oblast_cell) or ""
            mapping[(normalize(name), canon)] = code
    return mapping


INSERT_SQL = text(
    "INSERT INTO gazetteer (katottg, name_uk, name_ru, name_en, name_search, oblast, raion, "
    "population, geom) VALUES (:katottg, :name_uk, :name_ru, :name_en, :name_search, :oblast, "
    ":raion, :population, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))"
)
UPSERT_BY_KATOTTG = text(
    """
    INSERT INTO gazetteer (katottg, name_uk, name_ru, name_en, name_search, oblast, raion,
                           population, geom)
    VALUES (:katottg, :name_uk, :name_ru, :name_en, :name_search, :oblast, :raion,
            :population, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))
    ON CONFLICT (katottg) WHERE katottg IS NOT NULL
    DO UPDATE SET name_uk = EXCLUDED.name_uk, name_ru = EXCLUDED.name_ru,
                  name_en = EXCLUDED.name_en, name_search = EXCLUDED.name_search,
                  oblast = EXCLUDED.oblast, population = EXCLUDED.population,
                  geom = EXCLUDED.geom
    RETURNING (xmax = 0) AS was_insert
    """
)
# PLAN §8.5 gives (name_uk, oblast) as the no-KATOTTG key, but Ukrainian oblasts routinely hold
# several distinct villages of the same name — that key alone discarded ~25% of Donetsk oblast on
# first load. Proximity is added so a re-run matches the same settlement while genuinely
# different ones both survive.
FIND_EXISTING = text(
    "SELECT id FROM gazetteer WHERE name_uk = :name_uk "
    "AND coalesce(oblast,'') = coalesce(:oblast,'') "
    "AND ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, 3000) "
    "LIMIT 1"
)
UPDATE_EXISTING = text(
    "UPDATE gazetteer SET name_ru = :name_ru, name_en = :name_en, name_search = :name_search, "
    "population = :population, geom = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) WHERE id = :id"
)


def upsert_places(places: list[dict], katottg: dict) -> dict:
    inserted = updated = 0
    for i, place in enumerate(places, 1):
        params = {k: v for k, v in place.items() if k != "extra_names"}
        params["name_search"] = name_search_value(
            place["name_uk"], place.get("name_ru"), place.get("name_en"),
            *(place.get("extra_names") or []),
        )
        params["katottg"] = katottg.get((normalize(place["name_uk"]), place.get("oblast") or ""))

        if params["katottg"]:
            row = db.session.execute(UPSERT_BY_KATOTTG, params).first()
            if row and row.was_insert:
                inserted += 1
            else:
                updated += 1
        else:
            existing = db.session.execute(FIND_EXISTING, params).first()
            if existing:
                db.session.execute(UPDATE_EXISTING, {**params, "id": existing.id})
                updated += 1
            else:
                db.session.execute(INSERT_SQL, params)
                inserted += 1
        if i % 5000 == 0:
            db.session.commit()
            print(f"  … {i}/{len(places)}", flush=True)
    db.session.commit()
    return {"inserted": inserted, "updated": updated}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("geonames", "overpass"), default="geonames")
    parser.add_argument("--katottg", type=Path, help="KATOTTG codifier CSV (optional)")
    parser.add_argument("--fixture", action="store_true", help="load the offline test fixture only")
    parser.add_argument("--force-download", action="store_true", help="ignore the download cache")
    parser.add_argument("--only", help="overpass only: comma-separated ISO codes, e.g. UA-14")
    args = parser.parse_args()

    if args.fixture:
        if not FIXTURE.exists():
            print(f"error: fixture {FIXTURE} missing", file=sys.stderr)
            return 1
        places = orjson.loads(FIXTURE.read_bytes())
        print(f"fixture: {len(places)} places")
    elif args.source == "geonames":
        places = geonames_places(force=args.force_download)
        print(f"geonames: {len(places)} populated places")
    else:
        places = collect_overpass(args.only, args.force_download)

    if not places:
        print("error: no places collected", file=sys.stderr)
        return 1

    katottg = load_katottg(args.katottg) if args.katottg else {}
    if katottg:
        print(f"KATOTTG codes loaded: {len(katottg)}")

    app = create_app()
    with app.app_context():
        stats = upsert_places(places, katottg)
        total = db.session.execute(text("SELECT count(*) FROM gazetteer")).scalar()
        with_oblast = db.session.execute(
            text("SELECT count(*) FROM gazetteer WHERE oblast IS NOT NULL")
        ).scalar()
    print(f"gazetteer: {stats}, total rows {total} ({with_oblast} with an oblast)")
    if total < 25000 and not args.fixture:
        print("warning: PLAN §8.5 targets ≥ 25k settlements", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
