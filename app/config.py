"""Configuration: environment wiring plus the tunable application constants of PLAN §4.

Every constant is overridable by an environment variable of the same (upper-case) name so the
reference node can be tuned without a rebuild.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field, fields
from typing import Any

from dotenv import load_dotenv

load_dotenv()

_SENTINEL = object()


def _env(name: str, default: Any, cast=None):
    raw = os.environ.get(name, _SENTINEL)
    if raw is _SENTINEL:
        return default
    if cast is None:
        cast = type(default) if default is not None else str
    if cast is bool:
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if cast in (dict, list):
        return json.loads(raw)
    return cast(raw)


# --- war-relevance prefilter (PLAN §11) -------------------------------------------------------
# Matched case-insensitively against title+body before an item is worth LLM tokens.
RELEVANCE_PATTERNS: tuple[str, ...] = (
    # Ukrainian / Russian verbs and nouns of ground combat
    r"удар|обстріл|обстрел|наступ|наступл|просунул|просуну|звільни|освободи|захопи|захвати",
    r"штурм|окуп|оккуп|бої|боях|бой |боев|зачист|прорв|прорыв|відступ|отступ|контратак|контрнаступ",
    # Attack verbs. "атакувати/атаковать" is among the most common words in this corpus and its
    # absence alone accounted for a large share of a measured 17.5% prefilter miss rate.
    r"атак|уразил|ураже|вразил|знищ|уничтож|ліквідов|ликвидиров|підрив|подрыв|вибух|взрыв",
    # Losses and casualties — the vocabulary of equipment-loss and daily-tally reporting
    r"втрат|потер|загинул|погиб|поранен|ранен|полонен|пленн|збит|сбит|уламк|обломк",
    # Weapons / delivery systems ("беспилотник"/"безпілотник" is the standard word for drone and
    # was missing entirely)
    r"дрон|бпла|безпілот|беспилот|ракет|каб |кабів|шахед|shahed|himars|атакмс|atacms",
    r"іскандер|искандер|артилер|артиллер|міномет|миномет|фпв|fpv|планув|планир",
    # English
    r"\bfront ?line\b|\bfrontline\b|\badvance[ds]?\b|\bliberat|\bcaptur|\brecaptur|\boffensive\b",
    r"\bassault\b|\bshelling\b|\bshelled\b|\bmissile\b|\bdrone\b|\bstrike[sd]?\b|\bstruck\b",
    r"\bwithdrew\b|\bwithdraw|\bencircl|\bsalient\b|\bbridgehead\b|\bgrey zone\b|\bgray zone\b",
    r"\bgeolocat|\bconfirmed footage\b|\bdebunk|\bstaged\b|\bai[- ]generated\b|\bdeepfake\b",
    # English losses/attacks — equipment-loss feeds are written entirely in this register
    r"\bdestroyed\b|\bdamaged\b|\babandoned\b|\bloss(es)?\b|\bloses\b|\blost\b|\bcasualt",
    r"\battack(ed|s|ing)?\b|\bhit\b|\bexplosion\b|\bwounded\b|\bkilled\b|\btroops\b",
    r"\bartiller|\bmortar\b|\bUAV\b|\bglide bomb\b|\bair ?strike",
    # Place suffix heuristics: Ukrainian settlement/oblast morphology
    r"область|обл\.|оbласт|oblast|раїон|район|селищ|село|місто|город|напрямку|направлени",
    r"\w+(?:ivka|івка|овка|ovka|ське|sk|ськ|цьк|град|горск|горськ)\b",
    r"\braion\b|\bhromada\b|\bгромад",
)


# Cyrillic -> latin, one static table covering both the uk and ru schemes (PLAN §12.1).
TRANSLIT_TABLE: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
    "ю": "iu", "я": "ia", "ы": "y", "э": "e", "ё": "e", "ъ": "", "'": "", "’": "",
}


@dataclass(frozen=True)
class Settings:
    # --- infrastructure -----------------------------------------------------------------------
    database_url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL", "postgresql+psycopg://ukraine:ukraine@postgis:5432/ukraine"
        )
    )
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://redis:6379/0"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", ""))
    admin_username: str = field(default_factory=lambda: _env("ADMIN_USERNAME", ""))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", ""))
    public_poll_seconds: int = field(default_factory=lambda: _env("PUBLIC_POLL_SECONDS", 90))
    http_user_agent: str = field(
        default_factory=lambda: _env(
            "HTTP_USER_AGENT",
            "UkraineAggregator/1.0 (+https://github.com/cowolff/ukraine_aggregator)",
        )
    )

    # --- LLM proxy ----------------------------------------------------------------------------
    litellm_model: str = field(default_factory=lambda: _env("LITELLM_MODEL", ""))
    litellm_api_key: str = field(default_factory=lambda: _env("LITELLM_API_KEY", ""))
    litellm_api_base: str = field(default_factory=lambda: _env("LITELLM_API_BASE", ""))

    # --- algorithm constants (PLAN §4 table) --------------------------------------------------
    deep_strike_km: float = field(default_factory=lambda: _env("DEEP_STRIKE_KM", 30.0))
    claim_join_km: float = field(default_factory=lambda: _env("CLAIM_JOIN_KM", 5.0))
    # Radius of the grey halo drawn around an unconfirmed claim. A 3 km radius covers ~28 km²
    # per sighting, which swamped the map with circles far larger than the settlements they
    # describe; 1.5 km (~7 km²) is closer to a settlement's own footprint.
    grey_buffer_km: float = field(default_factory=lambda: _env("GREY_BUFFER_KM", 1.5))
    # How long a single sighting keeps painting grey. A geolocation that is never repeated is a
    # one-off report, not evidence of an ongoing contested area, so after this many days without
    # fresh corroboration the claim stops contributing to the grey zone. It stays `pending` in the
    # audit trail and can still be confirmed later; only its halo goes away. Distinct from
    # CLAIM_STALE_DAYS, which is when the claim itself is finally rejected.
    grey_claim_ttl_days: float = field(default_factory=lambda: _env("GREY_CLAIM_TTL_DAYS", 7.0))
    # Radius a *confirmed* claim contributes to the control layer when the gazetteer has no
    # settlement outline for it (PLAN §14.3's "or 2 km point buffer" fallback). Since GeoNames
    # ships no polygons, this is currently always the shape used.
    claim_apply_km: float = field(default_factory=lambda: _env("CLAIM_APPLY_KM", 2.0))
    # A report that a settlement changed hands names that settlement and perhaps its axis. A post
    # naming twenty or thirty is a daily situation summary — the General Staff's "231 clashes on
    # the front", the Russian MoD's daily bulletin, a "changes on the map" roundup. Extracting one
    # event per named place is right for the map, but such an item asserts no specific control
    # change, so it must not open or corroborate a frontline claim: two digests that both list the
    # same town would otherwise "corroborate" a change neither of them actually reports.
    # Measured break in the corpus: focused reports name 1-5 places, digests 6-36.
    claim_max_locations: int = field(default_factory=lambda: _env("CLAIM_MAX_LOCATIONS", 5))
    # Derived grey-zone parts below this area are digitization noise: where DeepState and ISW
    # trace the same line a few metres apart they leave thousands of slivers that carry ~1% of
    # the grey area but ~20% of its vertices. Set to 0 for the literal symmetric difference.
    grey_min_part_km2: float = field(default_factory=lambda: _env("GREY_MIN_PART_KM2", 0.5))
    gazetteer_min_similarity: float = field(
        default_factory=lambda: _env("GAZETTEER_MIN_SIMILARITY", 0.55)
    )
    llm_batch_size: int = field(default_factory=lambda: _env("LLM_BATCH_SIZE", 8))
    llm_max_retries: int = field(default_factory=lambda: _env("LLM_MAX_RETRIES", 3))
    llm_max_body_chars: int = field(default_factory=lambda: _env("LLM_MAX_BODY_CHARS", 4000))
    # Reasoning models (qwen3.x, o-series, …) spend a long time and many tokens thinking before
    # they answer: a trivial prompt measured ~20 s on the reference proxy, and a full batch runs
    # into minutes. The generic HTTP timeout is far too tight for that, so extraction gets its own.
    llm_timeout_s: float = field(default_factory=lambda: _env("LLM_TIMEOUT_S", 300.0))
    # Must leave room for the reasoning channel *plus* the JSON answer, or `content` comes back
    # empty while the whole budget is spent thinking.
    # Output budget reserved out of the context window (reasoning tokens + the JSON answer).
    llm_max_tokens: int = field(default_factory=lambda: _env("LLM_MAX_TOKENS", 2000))
    # Reasoning models burn their whole output budget thinking and then emit no answer at all on a
    # small context window. Extraction is a mechanical transcription task that gains nothing from a
    # chain of thought, so thinking is off by default: on the reference proxy this turned a 6.5 s
    # empty completion into a 0.4 s clean JSON one. Set LLM_REASONING=auto to leave it enabled.
    llm_reasoning: str = field(default_factory=lambda: _env("LLM_REASONING", "none"))
    # How long an extraction batch may hold its claim before `maintenance` assumes the worker died
    # and releases the rows. Must comfortably exceed the slowest batch.
    # How many LLM batches to keep queued. Sized a little above worker concurrency so a worker
    # never idles waiting for beat, while the queue stays short enough to stay responsive.
    llm_queue_target: int = field(default_factory=lambda: _env("LLM_QUEUE_TARGET", 12))
    # Ceiling on the default queue before poll fan-out backs off, so polling cannot crowd out
    # everything else on that queue.
    poll_queue_max: int = field(default_factory=lambda: _env("POLL_QUEUE_MAX", 120))
    llm_claim_stale_minutes: int = field(
        default_factory=lambda: _env("LLM_CLAIM_STALE_MINUTES", 30)
    )
    # Total context window. Discovered from the proxy's /models when it reports it; this is the
    # fallback and the ceiling. Input and output share it, so a batch must be sized to fit both.
    llm_context_tokens: int = field(default_factory=lambda: _env("LLM_CONTEXT_TOKENS", 8192))
    poll_jitter_s: int = field(default_factory=lambda: _env("POLL_JITTER_S", 30))
    snapshot_simplify_tolerances: dict = field(
        default_factory=lambda: _env(
            "SNAPSHOT_SIMPLIFY_TOLERANCES", {"low": 0.01, "mid": 0.002, "high": 0.0005}, dict
        )
    )

    # --- derived / operational ----------------------------------------------------------------
    http_timeout_s: float = field(default_factory=lambda: _env("HTTP_TIMEOUT_S", 20.0))
    api_cache_ttl_s: int = field(default_factory=lambda: _env("API_CACHE_TTL_S", 120))
    events_default_window_h: int = field(default_factory=lambda: _env("EVENTS_DEFAULT_WINDOW_H", 72))
    events_cluster_threshold: int = field(
        default_factory=lambda: _env("EVENTS_CLUSTER_THRESHOLD", 500)
    )
    claim_stale_days: int = field(default_factory=lambda: _env("CLAIM_STALE_DAYS", 14))
    low_confidence_floor: float = field(default_factory=lambda: _env("LOW_CONFIDENCE_FLOOR", 0.4))
    ukraine_bbox: tuple = (21.0, 43.0, 41.4, 53.6)  # lon_min, lat_min, lon_max, lat_max
    geoconfirmed_backfill_days: int = field(
        default_factory=lambda: _env("GEOCONFIRMED_BACKFILL_DAYS", 14)
    )
    snapshot_retention_days: int = field(
        default_factory=lambda: _env("SNAPSHOT_RETENTION_DAYS", 90)
    )
    testing: bool = field(default_factory=lambda: _env("TESTING", False, bool))

    def flask_config(self) -> dict:
        key = self.secret_key
        if not key:
            key = secrets.token_hex(32)
        return {
            "SECRET_KEY": key,
            "SQLALCHEMY_DATABASE_URI": self.database_url,
            "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5},
            "JSON_SORT_KEYS": False,
            "WTF_CSRF_TIME_LIMIT": None,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "TESTING": self.testing,
        }

    def as_dict(self) -> dict:
        out = {}
        for f in fields(self):
            if "key" in f.name or "password" in f.name:
                continue
            out[f.name] = getattr(self, f.name)
        return out


settings = Settings()

SECRET_KEY_IS_EPHEMERAL = not settings.secret_key
