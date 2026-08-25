"""Gazetteer matching — the hallucination firewall (PLAN §12).

No external geocoder is ever called. An event with neither a gazetteer match nor explicit
coordinates simply never gets a map position.
"""
from __future__ import annotations

import difflib
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

# Oblast aliases → canonical latin key used in the gazetteer's `oblast` column. This list does
# not try to enumerate every spelling — small variations ("Zaporizhia", "Kharkiw") are absorbed
# by the fuzzy fallback in canonical_oblast. What must be listed are different *word forms* the
# fuzzy match cannot bridge: adjectival vs noun (запорожская vs Запорожье) and the traditional
# Russian-derived English exonyms (Nikolaev, Chernigov).
OBLAST_ALIASES = {
    "donetsk": "donetsk", "донецька": "donetsk", "донецкая": "donetsk", "donetska": "donetsk",
    "донецьк": "donetsk", "донецк": "donetsk",
    "luhansk": "luhansk", "лугансь": "luhansk", "луганская": "luhansk", "lugansk": "luhansk",
    "луганськ": "luhansk", "луганск": "luhansk",
    "zaporizhzhia": "zaporizhzhia", "запорізька": "zaporizhzhia", "запорожская": "zaporizhzhia",
    "запоріжжя": "zaporizhzhia", "запорожье": "zaporizhzhia", "zaporozhye": "zaporizhzhia",
    "kherson": "kherson", "херсонська": "kherson", "херсонская": "kherson", "херсон": "kherson",
    "kharkiv": "kharkiv", "харківська": "kharkiv", "харьковская": "kharkiv", "kharkov": "kharkiv",
    "харків": "kharkiv", "харьков": "kharkiv",
    "dnipropetrovsk": "dnipropetrovsk", "дніпропетровська": "dnipropetrovsk",
    "днепропетровск": "dnipropetrovsk", "dnepropetrovsk": "dnipropetrovsk",
    "sumy": "sumy", "сумська": "sumy", "сумская": "sumy",
    "chernihiv": "chernihiv", "чернігівська": "chernihiv", "чернігів": "chernihiv",
    "чернигов": "chernihiv", "chernigov": "chernihiv",
    "mykolaiv": "mykolaiv", "миколаївська": "mykolaiv", "николаевская": "mykolaiv",
    "миколаїв": "mykolaiv", "николаев": "mykolaiv", "nikolaev": "mykolaiv",
    "odesa": "odesa", "одеська": "odesa", "одесская": "odesa", "odessa": "odesa",
    "одеса": "odesa", "одесса": "odesa",
    "kyiv": "kyiv", "київська": "kyiv", "киевская": "kyiv", "kiev": "kyiv", "київ": "kyiv",
    "crimea": "crimea", "крим": "crimea", "крым": "crimea", "ar krym": "crimea",
    "sevastopol": "sevastopol", "севастополь": "sevastopol",
    "poltava": "poltava", "полтавська": "poltava",
    "kirovohrad": "kirovohrad", "kirovograd": "kirovohrad",
    "cherkasy": "cherkasy", "vinnytsia": "vinnytsia", "vinnitsa": "vinnytsia",
    "zhytomyr": "zhytomyr", "zhitomir": "zhytomyr",
    "rivne": "rivne", "volyn": "volyn", "lviv": "lviv",
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


# Alias lookup keyed by the same normalization applied to inputs, so Cyrillic aliases match
# their own transliterations ("запорізька" and "zaporizka" are one entry, not two).
_NORM_ALIASES: dict[str, str] = {}
for _alias, _canon in OBLAST_ALIASES.items():
    _norm = normalize(_alias)
    if _norm:
        _NORM_ALIASES.setdefault(_norm, _canon)

# Fuzzy fallback: accept the closest alias when it is this similar to the input...
FUZZY_OBLAST_MIN_RATIO = 0.75
# ...and beats the best alias of any OTHER oblast by this margin. Guards the genuinely close
# pairs — a garbled "Chernig..." that scores chernihiv 0.78 and chernivtsi 0.74 stays
# unresolved rather than gambling on the wrong oblast.
FUZZY_OBLAST_MARGIN = 0.08


def _fuzzy_oblast(key: str) -> str | None:
    scored = sorted(
        ((difflib.SequenceMatcher(None, key, alias).ratio(), alias, canon)
         for alias, canon in _NORM_ALIASES.items()),
        reverse=True,
    )
    best_ratio, _, best_canon = scored[0]
    if best_ratio < FUZZY_OBLAST_MIN_RATIO:
        return None
    runner_up = next((r for r, _, canon in scored if canon != best_canon), 0.0)
    if best_ratio - runner_up < FUZZY_OBLAST_MARGIN:
        return None
    return best_canon


def canonical_oblast(value: str | None) -> str | None:
    """Map a free-form oblast mention to its canonical gazetteer key, or None.

    Exact alias lookup first, then a fuzzy pass so the LLM's spelling of the moment
    ("Zaporizhia", "Zaporizhzhya") still resolves instead of silently degrading the
    downstream match to a country-wide search.
    """
    if not value:
        return None
    key = normalize(value)
    if not key:
        return None
    tokens = key.split()
    for candidate in (key, *tokens):
        if candidate in _NORM_ALIASES:
            return _NORM_ALIASES[candidate]
    for candidate in (key, *(t for t in tokens if len(t) >= 5 and t != key)):
        canon = _fuzzy_oblast(candidate)
        if canon:
            return canon
    log.info("oblast hint %r did not canonicalize", value)
    return None


def name_search_value(*variants: str | None) -> str:
    """Concatenate every normalised name variant — the value stored in gazetteer.name_search."""
    seen: list[str] = []
    for variant in variants:
        norm = normalize(variant)
        if norm and norm not in seen:
            seen.append(norm)
    return " ".join(seen)


# Places outside Ukraine that war reporting names constantly (strike origins, targets of
# Ukrainian deep strikes, political datelines). The gazetteer only holds Ukrainian settlements,
# so without this guard these names fuzzy-match INTO Ukraine — an audit found Rostov placed in
# Zakarpattia and Orel in Odesa. Keys are normalize()d, so Cyrillic forms fold into the same
# entry. Events naming them simply stay unplaced.
NON_UKRAINE_PLACES = frozenset(
    normalize(name)
    for name in (
        # Russia — regions and cities that recur in strike reporting
        "Russia", "Росія", "Россия", "Российская Федерация",
        "Moscow", "Москва", "Moskva",
        "Rostov", "Rostov-on-Don", "Ростов-на-Дону", "Taganrog", "Таганрог",
        "Ростовская", "Ростовська",
        "Belgorod", "Бєлгород", "Белгород", "Shebekino", "Шебекіно", "Шебекино",
        "Белгородская", "Бєлгородська",
        "Bryansk", "Брянськ", "Брянск", "Брянская", "Брянська",
        "Kursk", "Курськ", "Курск", "Курская", "Курська",
        "Voronezh", "Воронеж", "Воронежская", "Воронезька",
        "Orel", "Oryol", "Орел", "Орёл", "Орловская", "Орловська",
        "Lipetsk", "Липецьк", "Липецк", "Tula", "Тула", "Kaluga", "Калуга",
        "Smolensk", "Смоленськ", "Смоленск", "Tver", "Твер", "Тверь",
        "Ryazan", "Рязань", "Pskov", "Псков", "Novgorod", "Новгород",
        "Krasnodar", "Краснодар", "Краснодарский", "Краснодарський",
        "Novorossiysk", "Новоросійськ", "Новороссийск",
        "Sochi", "Сочі", "Сочи", "Anapa", "Анапа",
        "Yeysk", "Єйськ", "Ейск", "Ейский", "Єйський", "Eysky",
        "Stavropol", "Ставрополь", "Volgograd", "Волгоград",
        "Saratov", "Саратов", "Engels", "Енгельс", "Энгельс",
        "Samara", "Самара", "Kazan", "Казань", "Tatarstan", "Татарстан",
        "Astrakhan", "Астрахань", "Saint Petersburg", "Санкт-Петербург",
        # Belarus
        "Belarus", "Білорусь", "Беларусь", "Minsk", "Мінськ", "Минск",
        "Gomel", "Homel", "Гомель", "Mazyr", "Мозир", "Мозырь", "Brest", "Брест",
    )
)


@dataclass
class Match:
    gazetteer_id: int
    name: str
    oblast: str | None
    similarity: float
    lat: float
    lon: float
    ambiguous: bool = False
    # How the match was made: "hinted" (found inside the LLM's oblast), "countrywide" (no
    # usable hint), or "fallback" (hinted search was empty; a near-exact country-wide hit).
    resolution: str = "countrywide"


# Population ratio above which the larger settlement wins outright instead of being reported as
# ambiguous. War reporting that says "Kostiantynivka" means the 78,000-person frontline city, not
# an 8-person hamlet of the same name three oblasts away.
POPULATION_DOMINANCE = 10.0

# Scores within this window of the best are treated as equally good matches.
SCORE_TIE_WINDOW = 0.05

# When a *hinted* search found nothing and the country-wide fallback runs, only a near-exact
# name hit may place the event. An empty hinted search means either the LLM's oblast was wrong
# or the place isn't a gazetteer settlement at all (a city district, a street, a garbled name);
# a fuzzy country-wide hit can't tell those apart and routinely snaps onto a like-named village
# in the wrong oblast ("Holosiivskyi", a Kyiv district, scored 0.62 against Гольмівський on the
# Donetsk frontline). Near-exact hits stay allowed so a merely-wrong hint doesn't unplace a
# unique, well-known settlement.
FALLBACK_MIN_SIMILARITY = 0.85


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
    if query in NON_UKRAINE_PLACES:
        log.info("place %r is outside Ukraine — staying unplaced", place_name)
        return None

    hint = canonical_oblast(oblast_hint)
    hint_given = bool(oblast_hint and str(oblast_hint).strip())
    fell_back = False
    rows = candidates(query, oblast=hint) if hint else []
    if not rows:
        # The country-wide fallback runs when there was no hint at all, when the hint didn't
        # canonicalize (often a foreign region — "Eysky District"), or when the hinted search
        # found nothing. In the latter two cases the extractor *claimed* to know the region and
        # that knowledge couldn't be used, so only a near-exact hit may place the event.
        fell_back = hint_given
        rows = candidates(query)
        hint = None
    if not rows:
        return None

    top_score = float(rows[0]["s"] or 0)
    if top_score < settings.gazetteer_min_similarity:
        return None
    if fell_back and top_score < FALLBACK_MIN_SIMILARITY:
        log.info(
            "place %r: no match in hinted oblast %r and country-wide best %.2f is below the "
            "fallback floor — staying unplaced",
            place_name,
            oblast_hint,
            top_score,
        )
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
        resolution="hinted" if hint else ("fallback" if fell_back else "countrywide"),
    )
