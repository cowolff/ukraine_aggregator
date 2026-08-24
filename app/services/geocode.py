"""Gazetteer matching — the hallucination firewall (PLAN §12).

No external geocoder is ever called. An event with neither a gazetteer match nor explicit
coordinates simply never gets a map position.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import text

from app.config import TRANSLIT_TABLE, settings
from app.extensions import db, log

_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)
_WS = re.compile(r"\s+")
# Prefixes/suffixes that carry no identity ("village of X", "смт X")
_NOISE_TOKENS = {
    "village", "town", "city", "settlement", "urban", "type", "district", "raion", "oblast",
    "region", "selo", "smt", "мiсто", "місто", "село", "селище", "смт", "район", "область",
    "муниципалитет", "поселок", "посёлок", "деревня", "город",
}

# Oblast aliases → canonical latin key used in the gazetteer's `oblast` column.
OBLAST_ALIASES = {
    "donetsk": "donetsk", "донецька": "donetsk", "донецкая": "donetsk", "donetska": "donetsk",
    "luhansk": "luhansk", "лугансь": "luhansk", "луганская": "luhansk", "lugansk": "luhansk",
    "zaporizhzhia": "zaporizhzhia", "запорізька": "zaporizhzhia", "запорожская": "zaporizhzhia",
    "kherson": "kherson", "херсонська": "kherson", "херсонская": "kherson",
    "kharkiv": "kharkiv", "харківська": "kharkiv", "харьковская": "kharkiv", "kharkov": "kharkiv",
    "dnipropetrovsk": "dnipropetrovsk", "дніпропетровська": "dnipropetrovsk",
    "sumy": "sumy", "сумська": "sumy", "сумская": "sumy",
    "chernihiv": "chernihiv", "чернігівська": "chernihiv",
    "mykolaiv": "mykolaiv", "миколаївська": "mykolaiv", "николаевская": "mykolaiv",
    "odesa": "odesa", "одеська": "odesa", "одесская": "odesa", "odessa": "odesa",
    "kyiv": "kyiv", "київська": "kyiv", "киевская": "kyiv", "kiev": "kyiv",
    "crimea": "crimea", "крим": "crimea", "крым": "crimea", "ar krym": "crimea",
    "sevastopol": "sevastopol", "севастополь": "sevastopol",
    "poltava": "poltava", "полтавська": "poltava",
    "kirovohrad": "kirovohrad", "cherkasy": "cherkasy", "vinnytsia": "vinnytsia",
    "zhytomyr": "zhytomyr", "rivne": "rivne", "volyn": "volyn", "lviv": "lviv",
    "ternopil": "ternopil", "khmelnytskyi": "khmelnytskyi", "chernivtsi": "chernivtsi",
    "ivano-frankivsk": "ivano-frankivsk", "zakarpattia": "zakarpattia",
}


def transliterate(value: str) -> str:
    """Cyrillic → latin using the single static table of PLAN §12.1 (uk and ru schemes)."""
    out = []
    for char in value:
        lowered = char.lower()
        if lowered in TRANSLIT_TABLE:
            mapped = TRANSLIT_TABLE[lowered]
            out.append(mapped)
        else:
            out.append(char)
    return "".join(out)


def normalize(value: str | None) -> str:
    """lowercase → strip punctuation → transliterate → drop noise tokens → collapse spaces."""
    if not value:
        return ""
    text_body = unicodedata.normalize("NFC", value).lower().strip()
    text_body = text_body.replace("ʼ", "").replace("’", "").replace("'", "")
    text_body = _PUNCT.sub(" ", text_body)
    tokens = [t for t in _WS.split(text_body) if t and t not in _NOISE_TOKENS]
    text_body = " ".join(tokens)
    text_body = transliterate(text_body)
    tokens = [t for t in _WS.split(text_body) if t and t not in _NOISE_TOKENS]
    return " ".join(tokens).strip()


def canonical_oblast(value: str | None) -> str | None:
    if not value:
        return None
    key = normalize(value)
    for token in (key, key.replace(" oblast", "").strip(), key.split()[0] if key else ""):
        if token in OBLAST_ALIASES:
            return OBLAST_ALIASES[token]
    key_translit = transliterate(value.lower())
    for alias, canon in OBLAST_ALIASES.items():
        if alias in key_translit or canon in key_translit:
            return canon
    return None


def name_search_value(*variants: str | None) -> str:
    """Concatenate every normalised name variant — the value stored in gazetteer.name_search."""
    seen: list[str] = []
    for variant in variants:
        norm = normalize(variant)
        if norm and norm not in seen:
            seen.append(norm)
    return " ".join(seen)


@dataclass
class Match:
    gazetteer_id: int
    name: str
    oblast: str | None
    similarity: float
    lat: float
    lon: float
    ambiguous: bool = False


# Population ratio above which the larger settlement wins outright instead of being reported as
# ambiguous. War reporting that says "Kostiantynivka" means the 78,000-person frontline city, not
# an 8-person hamlet of the same name three oblasts away.
POPULATION_DOMINANCE = 10.0

# Scores within this window of the best are treated as equally good matches.
SCORE_TIE_WINDOW = 0.05


def candidates(query: str, limit: int = 8, oblast: str | None = None) -> list[dict]:
    """Best gazetteer candidates for a normalised query.

    Scored with ``word_similarity``, not ``similarity``: ``name_search`` holds every name variant
    concatenated, so whole-string similarity penalises exactly the well-documented settlements it
    should favour — a full match on "kupiansk" scored 0.28 against a three-variant blob, and the
    Donetsk Kostiantynivka (pop. 78,179) lost to same-named hamlets purely for having more
    aliases. ``word_similarity`` scores the best-matching variant inside the blob instead, and is
    served by the same GIN trigram index.

    An oblast hint is applied in SQL rather than to an already-truncated candidate list, so the
    right settlement cannot be cut off before the filter runs.
    """
    oblast_clause = "AND oblast = :oblast" if oblast else ""
    sql = text(
        f"""
        SELECT id, name_uk, oblast, word_similarity(:q, name_search) AS s,
               ST_Y(geom) AS lat, ST_X(geom) AS lon, COALESCE(population, 0) AS pop
        FROM gazetteer
        WHERE :q <%% name_search
          {oblast_clause}
        ORDER BY s DESC, pop DESC
        LIMIT :limit
        """.replace("%%", "%")
    )
    params = {"q": query, "limit": limit}
    if oblast:
        params["oblast"] = oblast
    rows = db.session.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]


def match_place(place_name: str | None, oblast_hint: str | None = None) -> Match | None:
    """Resolve a free-text place name to a gazetteer point, or None (PLAN §12 steps 1-4)."""
    query = normalize(place_name)
    if len(query) < 3:
        return None

    hint = canonical_oblast(oblast_hint)
    rows = candidates(query, oblast=hint) if hint else []
    if not rows:
        # No hint, or the hint matched nothing: fall back to a country-wide search.
        rows = candidates(query)
        hint = None
    if not rows:
        return None

    top_score = float(rows[0]["s"] or 0)
    if top_score < settings.gazetteer_min_similarity:
        return None

    # word_similarity saturates at 1.0 for any exact variant hit, so several settlements routinely
    # tie. Within the tie window population is the best available prior: a report naming "Киев"
    # means the capital, not a like-named village that happened to sort first.
    tie_group = [r for r in rows if top_score - float(r["s"] or 0) <= SCORE_TIE_WINDOW]
    best = max(tie_group, key=lambda r: int(r["pop"] or 0))

    # Step 3: genuine near-ties in different oblasts with no oblast context stay unplaced. A
    # decisively larger settlement is not a genuine tie.
    rivals = [
        r
        for r in tie_group
        if r["id"] != best["id"]
        and (r["oblast"] or "") != (best["oblast"] or "")
        and int(best["pop"] or 0) < POPULATION_DOMINANCE * max(int(r["pop"] or 0), 1)
    ]
    ambiguous = bool(rivals) and not hint
    if ambiguous:
        log.info(
            "ambiguous place %r: %s vs %s",
            place_name,
            best["oblast"],
            sorted({r["oblast"] for r in rivals}),
        )

    return Match(
        gazetteer_id=best["id"],
        name=best["name_uk"],
        oblast=best["oblast"],
        similarity=float(best["s"]),
        lat=float(best["lat"]),
        lon=float(best["lon"]),
        ambiguous=ambiguous,
    )
