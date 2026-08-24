# Source Expansion Specification — new modalities, and an adapter architecture that scales

This document specifies (a) a refactor of the ingestion layer so that adding a source becomes a
one-file, zero-plumbing exercise, and (b) the integration of the next tranche of sources on top of
that refactor: sensor signals (NASA FIRMS fires, air-raid alerts, internet outages), a second and
third verified-geolocation stream (Eyes on Russia, Bellingcat civilian harm), a third independent
control line (UAControlMap), additional ISW ArcGIS layers, structured Russian-perspective feeds,
and offline calibration datasets (ACLED via HDX, VIINA).

It follows the conventions of `plans/PLAN.md`: it is written to be implementable without further
context, divergences go to `plans/DEVIATIONS.md`, and every external endpoint referenced here was
probed on **2026-08-24** unless marked otherwise (probe results in §16; move them into
`sources/VERIFIED_ENDPOINTS.md` as each integration lands).

Section numbering continues from the mental model of PLAN.md but is self-contained here.

---

## 1. Motivation and design goal

Today the system has **six adapter code paths and four of them are hand-plumbed**:

- `rss` and `telegram` sources flow through the generic scheduler
  (`tasks.dispatch_polls` → `tasks.poll_source` in `celery_worker/tasks/poll.py`), keyed off the
  `sources` table. Adding one is a DB row. Good.
- Every `api` source (`deepstate`, `isw`, `geoconfirmed`, `warspotting`) has its **own Celery task**
  (`pull_*` in `celery_worker/tasks/frontline.py`), its **own beat entry**
  (`celery_worker/celery_app.py`), its **own branch** in `fetch_source_preview`
  (`celery_worker/adapters/__init__.py`), its **own entry** in `API_ADAPTERS`, and — if it has
  history — its **own backfill function and CLI flag** in `scripts/backfill_history.py`. Adding one
  touches six files.
- `_ingest_geolocated` is duplicated between `celery_worker/tasks/frontline.py` and
  `scripts/backfill_history.py`.
- The frontline builder (`app/services/frontline.py::build`) is hard-wired to exactly two control
  providers (`deepstate`, `isw`) plus one declared-grey layer; a third opinion cannot be added
  without rewriting its SQL.

The design goal of Part I is a single sentence:

> **Adding a new source = writing one adapter module (with a fixture test) + inserting one
> `sources` row.** No new Celery task, no beat entry, no preview branch, no backfill flag, no
> ingest code.

Part II adds the one genuinely new concept the new sources need — **sensor signals**, a
non-editorial evidence modality — plus the N-provider generalization of the frontline builder.
Part III specifies each source against those interfaces. Parts IV–VII cover API/frontend, ops,
phasing, and the "add a source in 30 minutes" cookbook that should be true when this plan is done.

---

# Part I — The adapter architecture

## 2. The adapter contract

One protocol, four *output kinds*. The kind determines which **sink** persists the output; adapters
never touch the DB.

```python
# celery_worker/adapters/base.py
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field
from typing import Iterator, Protocol

@dataclass
class TextItem:            # → news_items, llm_status='pending'  (existing LLM pipeline)
    title: str | None
    body: str | None
    url: str | None = None
    external_id: str | None = None
    published_at: dt.datetime | None = None

@dataclass
class GeoEvent:            # → news_items(llm_status='skipped') + extracted_events with coords
    lat: float
    lon: float
    event_type: str        # one of models.events.EVENT_TYPES
    title: str | None
    body: str | None
    url: str | None = None
    external_id: str | None = None
    occurred_at: dt.datetime | None = None
    confidence: float = 0.9
    claimed_by: str | None = None          # 'ru' | 'ua' | None

@dataclass
class ControlLayer:        # → upstream_geometries via store_upstream()
    provider: str          # e.g. 'deepstate', 'isw', 'uacontrolmap', 'deepstate_grey'
    role: str              # 'control' | 'declared_grey'   (builder input semantics)
    geometries: list[dict] # GeoJSON geometry dicts (Polygon/MultiPolygon)
    version: str | None    # upstream change token (snapshot id, EditDate, KML mtime …)
    valid_at: dt.datetime | None = None
    plausible_km2: tuple[float, float] | None = None
    meta: dict = field(default_factory=dict)

@dataclass
class Signal:              # → sensor_signals (Part II) — non-editorial physical measurements
    kind: str              # 'fire' | 'air_alert' | 'net_outage' | …
    observed_at: dt.datetime
    lat: float | None = None               # point signals (FIRMS)
    lon: float | None = None
    region_code: str | None = None         # region signals (alerts, outages) — ISO-3166-2 UA-xx
    active: bool | None = None             # for interval signals: state transition edge
    intensity: float | None = None         # FRP for fires, score for outages
    external_id: str | None = None
    meta: dict = field(default_factory=dict)

@dataclass
class AdapterResult:
    items: list            # TextItem | GeoEvent | ControlLayer | Signal (homogeneous)
    state: dict = field(default_factory=dict)
    # state, unchanged semantics from today's poll(): 'etag', 'last_modified', 'not_modified',
    # 'empty_page', 'parsed_total', 'meta_update' — poll_source persists these onto the source row.

class Adapter(Protocol):
    name: str                                   # registry key == source.meta['adapter']
    kind: str                                   # 'text' | 'geo_events' | 'control' | 'signal'
    default_interval_s: int                     # used by seed script, overridable per row
    def poll(self, source, state: dict) -> AdapterResult: ...
    # OPTIONAL — implement to opt in to `scripts/backfill_history.py --source <name>`:
    # def backfill(self, source, since: dt.datetime, until: dt.datetime | None,
    #              budget: "BackfillBudget") -> Iterator[AdapterResult]: ...
    # OPTIONAL — override when the first 3 poll items are not a useful admin preview:
    # def preview(self, source) -> tuple[list[dict], str]: ...
```

Notes, all deliberate:

- **`state` in, `state` out.** The adapter receives the persisted per-source cursor
  (`source.etag`, `source.last_modified`, `source.meta` — assembled by the caller) and returns
  deltas. Adapters stay stateless and unit-testable against fixtures with no DB.
- **Homogeneous `items`.** An upstream that yields two things (DeepState: occupied polygons *and*
  declared-grey polygons) returns **two `ControlLayer` items in one result** — the sink handles a
  list, so this needs no special casing (the existing `deepstate` / `deepstate_grey` provider split
  is preserved exactly).
- **`backfill` is an iterator of the same result type** — the generic backfill driver is a loop of
  "sink each result, commit, honor `budget.pause`". `BackfillBudget` carries
  `pause_s`, `max_calls`, `per_day` — the knobs `scripts/backfill_history.py` already has.
- **No inheritance requirement.** A module-level object (or the module itself, matching today's
  `rss.py` style) satisfying the protocol is fine. Keep it duck-typed like the existing code.

## 3. The registry

```python
# celery_worker/adapters/registry.py
ADAPTERS: dict[str, Adapter] = {}

def register(adapter: Adapter) -> Adapter:
    if adapter.name in ADAPTERS:
        raise RuntimeError(f"duplicate adapter {adapter.name!r}")
    ADAPTERS[adapter.name] = adapter
    return adapter

def resolve(source) -> Adapter | None:
    # meta['adapter'] wins; fall back to type for legacy rows ('rss', 'telegram').
    key = (source.meta or {}).get("adapter") or source.type
    return ADAPTERS.get(key)
```

`celery_worker/adapters/__init__.py` shrinks to importing every adapter module (imports run
`register(...)`) and re-exporting `resolve`. `fetch_source_preview` becomes:

```python
def fetch_source_preview(source):
    adapter = resolve(source)
    if adapter is None:
        return [], f"no adapter registered for {source.type!r}/{(source.meta or {}).get('adapter')!r}"
    if hasattr(adapter, "preview"):
        return adapter.preview(source)
    result = adapter.poll(source, _cursor_state(source))
    return [_preview_dict(i) for i in result.items[:3]], _preview_note(result.state)
```

**Existing adapters are wrapped, not rewritten.** `rss.py` and `telegram_web.py` already have the
right shape — give each a thin `poll(source, state)` that returns `AdapterResult([TextItem(...)…])`.
`deepstate.fetch_last`, `isw_arcgis.fetch_control`, `geoconfirmed.fetch_recent`,
`warspotting.fetch_recent/fetch_backfill` keep their current functions (tests keep passing) and gain
a registered wrapper object that adapts them to the protocol.

## 4. One scheduler, one poll task, four sinks

### 4.1 Dispatch

`tasks.dispatch_polls` drops its `type IN ('rss','telegram')` restriction and dispatches **every
enabled, due source that resolves to an adapter**. The per-type beat entries
(`pull-deepstate`, `pull-isw`, `pull-warspotting`, `pull-geoconfirmed`) are **deleted**; their
cadences move to `poll_interval_s` on the source rows (deepstate 1800, isw 3600, warspotting 3600,
geoconfirmed 86400 — geoconfirmed's "run at 04:00" beat crontab becomes "once per 86400 s", which
is equivalent for a daily full-export diff and removes the special case).

Two scheduler refinements this newly requires:

- **Queue routing by cost.** Bulk pulls (geoconfirmed CSV, FIRMS archive) should not head-of-line
  block a Telegram poll. Add `source.meta['queue'] = 'bulk'` (default `default`), and route in
  `poll_source.apply_async(..., queue=...)`. Declare the `bulk` queue on the existing worker —
  no new container on the 4 GB node, just a second `-Q` entry.
- **Per-source soft time limit.** `poll_source` keeps the global 600 s soft limit; bulk adapters
  must stream to the scratchpad (the `stream_to_file` helper already exists in
  `celery_worker/adapters/http.py`).

### 4.2 The generic poll task

`tasks.poll_source` keeps its exact failure-accounting contract — 403 ⇒ degraded-never-dead,
5 failures ⇒ degraded, 20 ⇒ dead, `empty_page` markup-drift counter, `not_modified` fast path —
and replaces its two-way type branch with:

```python
adapter = resolve(source)
result  = adapter.poll(source, _cursor_state(source))     # errors handled as today
inserted = SINKS[adapter.kind](source, result.items)
```

### 4.3 Sinks (`celery_worker/sinks.py`, new)

| kind         | sink                                                                                              | dedup key |
|--------------|---------------------------------------------------------------------------------------------------|-----------|
| `text`       | today's `ingest_items` (moved here unchanged)                                                       | `content_hash(title, body)` |
| `geo_events` | today's `_ingest_geolocated` (moved here, **de-duplicated with the copy in `scripts/backfill_history.py`** — both import this one) | `content_hash`; plus `(source_id, external_id)` short-circuit |
| `control`    | `store_upstream(provider, geometries, version, meta, plausible_km2, valid_at)` + `mark_frontline_dirty()`; skips when `latest_upstream(provider).upstream_version == version` (the "unchanged" fast path from `pull_deepstate`/`pull_isw`, now generic) | `(provider, upstream_version)` |
| `signal`     | new `ingest_signals` (Part II §8)                                                                   | `(source_id, external_id)` unique index |

The `geo_events` sink takes `event_type`, `confidence`, `claimed_by`, `occurred_at` **from each
`GeoEvent`** rather than from a task-level argument — this is what lets one adapter emit
`geolocation_proof` and another `civilian_harm` through identical plumbing.

### 4.4 Source model and migration

Schema changes (one Alembic migration, backwards-compatible):

1. Widen the `sources.type` CHECK constraint: `('rss','telegram','api','scrape','sensor','dataset')`.
   `type` becomes **descriptive** (admin filter facet); `meta['adapter']` is what routes. Existing
   rows are untouched — `resolve()` falls back to `type` for `rss`/`telegram`.
2. `sources.meta` gains optional, documented keys (JSONB — no DDL): `adapter`, `queue`,
   `secret_env` (§5), plus adapter-private cursor keys (each adapter must namespace:
   `firms_last_ts`, `eor_last_id`, …).
3. **No secrets column.** See §5.

Also delete `PROVIDERS` in `app/services/frontline.py` (unused constant today; superseded by the
`control_providers` config of §9).

## 5. Secrets pattern

Some new upstreams need keys (FIRMS `MAP_KEY`, alerts.in.ua token, ukrainealarm.com key,
Cloudflare Radar token). Rule: **secrets live only in the environment; the DB names them.**

- `source.meta['secret_env'] = "FIRMS_MAP_KEY"` — the adapter reads
  `os.environ[source.meta['secret_env']]` via one helper `adapters.base.secret_for(source)` which
  raises a `FetchError("secret env FIRMS_MAP_KEY unset")` (⇒ normal degraded accounting, and the
  admin *test fetch* button shows exactly what is missing).
- `.env.example` gains commented entries for each (§16 lists them).
- The admin sources form never displays or stores key material; backups (`pg_dump`) therefore never
  contain credentials.

## 6. Generic backfill

`scripts/backfill_history.py` keeps its DeepState `--frontline` mode (it drives snapshot *rebuilds*,
not just ingestion, and stays special) and replaces the per-source functions/flags with:

```
python scripts/backfill_history.py --source <adapter-name> --since 2025-01-01 [--until …]
                                   [--pause 0.4] [--max-calls N]
python scripts/backfill_history.py --list        # adapters that implement backfill, with notes
```

Driver: resolve adapter → require `backfill` → iterate `AdapterResult`s → same sink as live →
commit per result → progress line per iteration. Existing `--geoconfirmed/--warspotting/--telegram`
flags become aliases for `--source …` for one release, then are removed (note in DEVIATIONS.md).

## 7. Contract tests — the enforcement mechanism

The reason future sources stay cheap is that the test suite is **parametrized over the registry**:

```python
# tests/test_adapter_contract.py
@pytest.mark.parametrize("adapter", ADAPTERS.values(), ids=lambda a: a.name)
def test_contract(adapter, fixture_source, respx_mock):
    mount_fixtures(respx_mock, adapter.name)          # tests/fixtures/adapters/<name>/*.json|xml|csv
    result = adapter.poll(fixture_source, {})
    assert isinstance(result, AdapterResult)
    assert all(isinstance(i, KIND_TYPES[adapter.kind]) for i in result.items)
    assert result.items, "fixture must produce at least one item"
    # kind-specific invariants:
    #   geo_events → lat/lon inside settings.ukraine_bbox OR adapter declares allow_out_of_bbox
    #   geo_events/text → external_id or (title|body) present; published/occurred tz-aware
    #   control → geometries parse via shapely, version non-empty
    #   signal  → observed_at tz-aware; point XOR region_code
```

A new adapter that ships without a fixture directory fails CI by construction. Each adapter module
additionally gets its own parsing-edge-case tests (as `warspotting`'s tags-as-string handling has
today).

**Definition of done for Part I:** behavior-preserving — same rows ingested for the four existing
API sources on a replayed fixture day, `pull_*` tasks deleted, beat schedule reduced to
`dispatch-polls` + `llm-extract` + `geocode-pending` + `evaluate-claims` + `rebuild-frontline`(+nightly)
+ `maintenance`, and both copies of `_ingest_geolocated` collapsed into one.

---

# Part II — New evidence infrastructure

## 8. Sensor signals: a non-editorial modality

FIRMS fire detections, air-raid alerts and connectivity outages are **physical measurements, not
reports**. They must not enter `news_items` (they'd pollute the feed, the LLM queue, and dedup) and
they must not carry a perspective (nobody "said" them). New tables:

```sql
CREATE TABLE regions (                         -- 27 oblasts + Kyiv city; geoBoundaries ADM1
    id            serial PRIMARY KEY,
    code          text UNIQUE NOT NULL,        -- ISO-3166-2, e.g. 'UA-63'
    name_en       text NOT NULL,
    name_uk       text NOT NULL,
    boundary      geometry(MultiPolygon, 4326) NOT NULL
);

CREATE TABLE sensor_signals (                  -- point-in-time detections (FIRMS)
    id            bigserial PRIMARY KEY,
    source_id     int NOT NULL REFERENCES sources(id),
    kind          text NOT NULL,               -- 'fire' | …
    geom          geometry(Point, 4326),
    observed_at   timestamptz NOT NULL,
    intensity     real,                        -- FRP (MW) for fires
    external_id   text,
    meta          jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, external_id)
);
CREATE INDEX ix_signals_kind_observed ON sensor_signals (kind, observed_at DESC);
CREATE INDEX ix_signals_geom ON sensor_signals USING gist (geom);

CREATE TABLE region_status_intervals (         -- stateful region signals (alerts, outages)
    id            bigserial PRIMARY KEY,
    source_id     int NOT NULL REFERENCES sources(id),
    kind          text NOT NULL,               -- 'air_alert' | 'net_outage'
    region_id     int NOT NULL REFERENCES regions(id),
    started_at    timestamptz NOT NULL,
    ended_at      timestamptz,                 -- NULL = currently active
    meta          jsonb NOT NULL DEFAULT '{}',
    UNIQUE (source_id, kind, region_id, started_at)
);
CREATE INDEX ix_region_status_active ON region_status_intervals (kind, region_id)
    WHERE ended_at IS NULL;
```

`ingest_signals` (the `signal` sink): point signals insert-or-skip on `(source_id, external_id)`;
region signals are edge-driven — a `Signal(active=True)` opens an interval if none is open, a
`Signal(active=False)` closes the open one (`ended_at = observed_at`). Idempotent by construction,
so live polling and backfill share it.

`regions` is seeded by a new idempotent `scripts/seed_regions.py` from
**geoBoundaries UKR ADM1** (open license, ~1 MB simplified). Region-code mapping tables for each
alert upstream live in the adapter (they name oblasts in Ukrainian).

### 8.1 What sensors are allowed to influence — the load-bearing decision

**Sensor signals never confirm claims and never move the frontline.** Rationale, to be kept next to
the code: a fire proves *combustion*, an alert proves *a siren* — neither proves **who controls
ground**, and the confirmation rule (`app/services/rules.py::_confirmation`) is exactly a rule
about control. Folding sensors into the ≥2-perspectives test (e.g. as a fifth class) would let
"Ukrainian claim + thermal anomaly" flip territory — a category error and an obvious poisoning
vector (set a field on fire, post a claim).

What sensors **do**:

1. **Map layers** of their own (§12) — that alone justifies them.
2. **Event corroboration badges.** A new nightly-and-on-demand job `tasks.corroborate_events`
   stamps `extracted_events` rows of type `shelling` / `deep_strike` / `other` with
   `meta_corroboration` (new JSONB column on `extracted_events`, nullable):
   `{"fire": {"count": 3, "min_km": 1.2, "window_h": 12}, "air_alert": true}` when a FIRMS
   detection lies within `SENSOR_CORROBORATION_KM` (default 5) and ±12 h, or the event's oblast had
   an active alert at `occurred_at`. Rendered as a small "sensor-corroborated" badge in the popup
   and available as an `/api/events` filter. It changes **display prominence, never rule outcomes**.
3. **Context rows in the evidence chain.** `evidence_chain()` (admin claims review) appends a
   read-only "sensor context" section computed on the fly (fires within 10 km of the claim in the
   claim's active window). No `evidence_links` rows — `_confirmation` iterates links, and sensor
   context must be structurally invisible to it.

This is deliberately conservative; if experience shows sensor context is highly predictive,
loosening it is a one-line change in `_confirmation` *later*, with data to justify it.

## 9. N-provider frontline builder

`build()` currently hard-codes `ds`/`isw` CTEs. Generalize:

```python
# app/config.py
control_providers: dict = field(default_factory=lambda: _env(
    "CONTROL_PROVIDERS",
    {"deepstate": {"required": False}, "isw": {"required": False},
     # "uacontrolmap": {"required": False},        # enabled in Phase 3
    }, dict))
agreement_quorum: int = field(default_factory=lambda: _env("AGREEMENT_QUORUM", 0))
# 0 = unanimity (today's semantics generalized): RU = ∩ fresh layers, grey ⊇ (∪ − ∩)
# k>0 = majority: RU = area claimed by ≥k fresh layers, grey ⊇ (∪ − RU)
```

SQL is assembled over the fresh subset `L₁…Lₙ` of configured providers (a provider with no stored
geometry, or stale beyond `UPSTREAM_STALE_HOURS`, drops out and is recorded in
`generation_meta.degraded`, generalizing today's `single_upstream:<p>`):

- **Unanimity (default, n≤3):** `agreed = ST_Intersection` folded across layers;
  `disagreed = ST_Difference(ST_Union(all), agreed)`. With n=2 this reduces *exactly* to today's
  `ST_Intersection` / `ST_SymDifference` output, which is the regression test.
- **Quorum k (opt-in):** per-layer dump to a `parts` CTE, `ST_Union` of areas covered by ≥k layers
  via pairwise intersections for n=3 (`(A∩B) ∪ (A∩C) ∪ (B∩C)` for k=2 — implement the n=3 closed
  form, not a general n; n>3 is out of scope and rejected at config load).
- Everything downstream — sliver filter, declared-grey union, claim add/cut, pending buffers —
  is untouched: it already operates on `agreed`/`disagreed`.
- `generation_meta` records per-provider `{provider: {version, fetched_at, area_km2}}` instead of
  the current fixed `deepstate_*`/`isw_*` keys; keep the old keys populated too for one release so
  the SPA's meta panel doesn't break (DEVIATIONS.md note when removed).

Snapshot plausibility guard: with three providers, also log (not block) when any pairwise area
disagreement exceeds 15% — that is the "one map went crazy" alarm, surfaced on the admin dashboard.

## 10. Event-type additions

`EVENT_TYPES` (in `app/models/events.py`, CHECK-constrained) gains:

- `civilian_harm` — glyph `✚`, icon-only (never in `FRONTLINE_EVENT_TYPES`, excluded from claim
  intake exactly like `deep_strike` far-behind events; add to the icon-only guard in
  `rules._intake_one`).

The LLM extraction prompt/schema is **not** taught the new type (it arrives only via structured
adapters with explicit coordinates); the extraction enum stays as-is, which avoids re-validating
prompt behavior. Migration: widen the CHECK constraint; add glyph + colour to `/api/config` meta.

---

# Part III — Per-source integration specs

Ordered by (value ÷ effort), which is also roughly the build order of §14. Every source below gets
a row in `sources/ukraine_frontline_sources.csv` (if missing) and a probe log block in
`sources/VERIFIED_ENDPOINTS.md`.

## 11.1 NASA FIRMS active fires — adapter `firms`, kind `signal`

*The highest-value single addition: a physical, non-party corroboration layer, pollable and
backfillable.*

- **Access:** free `MAP_KEY` (instant self-service at
  `https://firms.modaps.eosdis.nasa.gov/api/map_key/`), env `FIRMS_MAP_KEY`,
  `meta.secret_env` per §5. Quota: 5000 transactions / 10 min — irrelevant at our cadence (≤2/h).
- **Live poll** (interval 1800 s, queue `default`):
  `GET https://firms.modaps.eosdis.nasa.gov/api/area/csv/{KEY}/VIIRS_SNPP_NRT/{w},{s},{e},{n}/1`
  with bbox = `settings.ukraine_bbox` (extend east to 42.5 to cover Belgorod/Kursk border strips —
  add `FIRMS_BBOX` setting defaulting to the extended box). Also poll `VIIRS_NOAA20_NRT` and
  `VIIRS_NOAA21_NRT` (three satellites ≈ 6+ overpasses/day). CSV columns:
  `latitude, longitude, bright_ti4, acq_date, acq_time, satellite, confidence, frp, daynight`.
- **Mapping:** one `Signal(kind='fire')` per row; `observed_at = acq_date+acq_time (UTC)`;
  `intensity = frp`; `external_id = f"{satellite}:{acq_date}:{acq_time}:{lat:.4f}:{lon:.4f}"`
  (FIRMS has no row id; this tuple is the natural key and makes NRT→standard-product re-delivery
  idempotent). Drop `confidence == 'l'` (low) rows. **Do not pre-filter to the frontline buffer at
  ingest** — the corroboration job and the map layer filter at read time; raw Ukraine-wide data is
  ~200–2000 rows/day (heavier in burn season), trivially storable.
- **Backfill:** `GET /api/area/csv/{KEY}/VIIRS_SNPP_SP/{bbox}/{n}/{date}` walks standard-product
  history 10 days per call; the archive downloads (yearly country CSVs) cover 2022–2024 for a bulk
  seed. Implement `backfill(since, until)` over the date-chunk endpoint; archive files are a
  documented manual step, not code.
- **Known noise:** agricultural burns (May–September, huge), flare stacks, sun glint. Mitigations
  are consumer-side (§8.1 badge requires proximity to *an existing reported event*; the map layer
  defaults to a frontline-buffer filter with a "show all" toggle) — noise is why FIRMS never
  confirms anything.
- **Licensing/attribution:** NASA LANCE/FIRMS — free, requires the standard citation; add to the
  README attribution block.

## 11.2 Air-raid alerts — adapter `air_alerts`, kind `signal`

- **Primary (key-free, verified 200 today):** `GET https://ubilling.net.ua/aerialalerts/` →
  `{"source": …, "cachedat": …, "states": {"<oblast name UA>": {"alertnow": bool, "changed": ts}}}`.
  Oblast granularity only. Poll interval 120 s (it is a cache itself, `cachedat` shows staleness).
- **Official upgrades (both catalogued as needing tokens in VERIFIED_ENDPOINTS.md):**
  `alerts.in.ua` (`ALERTS_IN_UA_TOKEN`, free for non-commercial on request) adds raion/hromada
  granularity + a history endpoint (→ `backfill`); `ukrainealarm.com` (`UKRAINEALARM_KEY`) is the
  fallback official API. Implement the adapter with a `variant` in `source.meta`
  (`ubilling` | `alerts_in_ua` | `ukrainealarm`) so all three are **one adapter, three source
  rows** (enable exactly one at a time; the region mapping differs per variant). **Request both
  tokens at project start** — turnaround is days and gates the history backfill.
- **Mapping:** compare `states` against open intervals per §8; emit edge `Signal`s
  (`kind='air_alert'`, `region_code` via the adapter's oblast-name→ISO map, `observed_at =
  changed`). The `changed` timestamps are Kyiv local time — convert from `Europe/Kyiv` explicitly.
- **Consumers:** live overlay (§12), `air_alert: true` corroboration badge (§8.1), and the
  timeline scrubber can shade alert-active periods per oblast.
- **Caveat to record:** oblast-level alerts fire for *air threats in transit*, not local combat —
  the badge copy must say "alert active in oblast", nothing stronger.

## 11.3 Eyes on Russia (Centre for Information Resilience) — adapter `eyes_on_russia`, kind `geo_events`

*The redundancy play: the single most load-bearing rule (`rule:geoproof`) currently rests on one
provider, GeoConfirmed.*

- **Access:** the map at `eyesonrussia.org/map` is an SPA; `/events.json` and `/api/events`
  return the app shell (probed today). **First implementation step is endpoint discovery via
  browser DevTools** (same exercise as GeoConfirmed's Blazor app); the map is known to load a
  single events GeoJSON/JSON payload. If the endpoint proves unstable or access-restricted, fall
  back to their published dataset exports and run it as a `dataset`-type weekly source. Budget this
  uncertainty: the adapter is trivial, the discovery is the work. Record whatever is found in
  VERIFIED_ENDPOINTS.md with response shapes.
- **Mapping:** `GeoEvent(event_type='geolocation_proof', confidence=1.0, occurred_at=<their event
  date>, url=<their permalink or underlying media URL>, external_id=<their id>)`. Perspective of
  the source row: `neutral`, tier 1 — same posture as GeoConfirmed.
- **Cross-verifier duplicate policy (decide now, it touches the rules engine):** GeoConfirmed and
  EoR frequently geolocate the *same footage*. Two proofs of one claim are harmless
  (`rule:geoproof` fires once) — **but a debunk must retract both**. `rules._debunk_targets`
  matching is by URL / settlement / 10 km radius and therefore already catches co-located proofs
  from both providers; add a test asserting exactly that (debunk retracts GeoConfirmed *and* EoR
  proof at the same settlement). No dedup at ingest — provenance is the product.
- **Backfill:** their dataset reaches to Feb 2022; implement `backfill` over the same payload
  (it is historically complete in one export, like GeoConfirmed's CSV).

## 11.4 Bellingcat civilian-harm dataset — adapter `bellingcat_civharm`, kind `geo_events`

- **Access:** the original TimeMap API paths 404 today (probed). Current distribution is the
  published dataset (CSV/JSON export from `ukraine.bellingcat.com` / GitHub mirror). Treat as a
  **daily `dataset` source**: fetch export, diff by `external_id` (their incident id `CIV0001`-style),
  emit new rows. If only bulk CSV exists, the conditional-GET `etag` path makes daily refetch cheap.
- **Mapping:** `GeoEvent(event_type='civilian_harm', confidence=0.9, claimed_by=None)`. Source row:
  perspective `neutral` (their methodology is verification-based), tier 1, but the event type is
  icon-only by §10 — civilian-harm reports must never enter frontline claims.
- **Value:** a distinct map layer with real editorial weight, plus overlap checks against our own
  extracted `shelling`/`deep_strike` events (a civilian-harm pin near an extracted strike event is
  mutual corroboration — surfaced in popups, again display-only).
- **Licensing:** verify the current license before shipping the layer publicly (was CC-BY-style
  with attribution; if redistribution is restricted, ship admin-only, same posture as ACLED §11.8).

## 11.5 UAControlMap third control line — adapter `uacontrolmap`, kind `control`

- **Access:** the community control map (Poulet volant / UAControlMap) is maintained as Google
  MyMaps; a stable KML export URL exists per map id:
  `https://www.google.com/maps/d/kml?mid=<MID>&forcekml=1`. Probe and pin the MID at
  implementation time; `forcekml=1` yields plain KML (not KMZ). Parse with `fastkml` or a 40-line
  expat walk (polygons only, folder names carry the control classification — verify actual folder
  taxonomy at implementation and record it).
- **Mapping:** `ControlLayer(provider='uacontrolmap', role='control', version=<Last-Modified or
  content hash>, plausible_km2=<same band as deepstate>)`. Enable as the third entry in
  `control_providers`; **run for ≥2 weeks with `AGREEMENT_QUORUM=0` (unanimity)** and watch the
  admin pairwise-area panel (§9) before considering quorum 2-of-3 — unanimity with a third source
  strictly grows the grey zone (honest), quorum can *shrink* it (needs evidence the third line is
  trustworthy).
- **Risks:** MyMaps has no versioning contract; markup drift ⇒ the plausibility band and the
  degraded path already contain the blast radius (a failed parse just drops it from the fresh set).
  Poll interval 3600 s. Do not backfill (no history exists).

## 11.6 Additional ISW ArcGIS layers — no new adapter

`isw_arcgis.py` is parameterized by `meta.service`/`meta.layer` already; these are **new source
rows only** (after Part I, each is independently schedulable):

| service (verified enumerable in VERIFIED_ENDPOINTS.md)      | use                                | sink semantics |
|--------------------------------------------------------------|------------------------------------|----------------|
| `Assessed_Russian_Gains_in_the_Past_24_Hours_view/…/0`        | 24 h-gains **event overlay**       | `geo_events`: polygon centroid → `GeoEvent(event_type='frontline_advance', claimed_by='ru', confidence=0.9)` — feeds claims as western-perspective support, which is exactly what ISW assessments are |
| `VIEW_ClaimedRussianTerritoryinUkraine_V2`                    | claimed-vs-assessed **grey input** | `ControlLayer(provider='isw_claimed', role='declared_grey')`: builder change — `declared_grey` generalizes from the single `deepstate_grey` row to the union of all fresh `role='declared_grey'` providers (small edit in `build()`, covered by the §9 rework). Claimed−assessed *is* a disagreement band stated by the source itself |
| `RUAF_Field_Fortifications_Polylines/…/0`                     | static **fortifications overlay**  | not events, not control: store as a new `overlay` upstream provider, served raw via `/api/overlays/fortifications` (simplified once at ingest; changes rarely) |

The centroid rule for 24 h-gains needs one guard: skip polygons whose area exceeds 500 km²
(occasional service glitches publish the whole theatre; centroid would be meaningless).

## 11.7 Structured Russian-perspective sources — mostly ops, one small adapter

The confirmation rule needs the Russian class to be *reportable in machine-readable form*, or
RU-claimed advances structurally under-confirm relative to UA-claimed ones (asymmetric bias).

1. **Ops, no code (do first):** enable the already-catalogued Russian Telegram channels via the
   existing `telegram_web` adapter — Rybar, Two Majors, WarGonzo mirrors and the `mod_russia`
   channel are in the CSV. Target: **every active frontline axis has ≥1 enabled Russian-perspective
   poller**; verify with a new admin "perspective coverage by axis" note (manual review is fine —
   just enable and watch the extraction quality for a week). All tier 3 by default ⇒ they can never
   confirm alone (`rules._confirmation` excludes tier 3) — they exist to *state the Russian claim*,
   which the grey zone renders.
2. **RSS, no code:** TASS / RIA Novosti feeds through the existing `rss` adapter, tier 3,
   perspective `russian`. The relevance regex already covers the Russian combat vocabulary.
3. **One new generic adapter `html_list`, kind `text`:** a selector-driven scraper for the many
   catalogued `scrape` sources that today hit the "no adapter yet" branch. Config entirely in
   `source.meta`: `{"adapter":"html_list", "item_selector": css, "title_selector": css,
   "body_selector": css, "link_selector": css, "date_selector": css, "date_format": strptime}`.
   First user: mil.ru daily briefings (Russian MoD summary — the canonical Russian official claim
   stream; it geo-names settlements constantly, which is exactly what the gazetteer pipeline eats).
   `html_list` inherits the markup-drift detector semantics (`empty_page` counter) from the
   Telegram path — same protection, no new mechanism. This one adapter converts a whole CSV
   category from "catalogued" to "pollable".

## 11.8 Calibration datasets: ACLED (HDX mirror) and VIINA — adapter kind `dataset`, admin-only

These are **not live sources and never touch the map or the rules engine**. Purpose: recall
measurement — "where is fighting that our pipeline doesn't see?"

- New table `benchmark_counts (dataset text, adm2_code text, week date, event_count int,
  UNIQUE(dataset, adm2_code, week))`, plus adm2 codes on `regions`-style lookup (geoBoundaries ADM2,
  seeded by the same `seed_regions.py`).
- **ACLED:** weekly task pulls the HDX Ukraine mirror (package API verified 200 today:
  `data.humdata.org/api/3/action/package_show?id=ukraine-acled-conflict-data` — resolve the
  current resource URL from the package payload, don't pin the file URL), aggregates to
  adm2×week. **License:** ACLED terms prohibit redistribution — aggregates render on the *admin*
  dashboard only, never on the public API. Enforce structurally: the ingest writes only
  `benchmark_counts` (aggregates), raw rows are discarded.
- **VIINA (Zhukov, GitHub):** yearly zips under `Data/` (verified listing today; files are
  **Git-LFS pointers** — fetch via the LFS media URL, i.e. follow `raw.githubusercontent.com` →
  LFS redirect or use the `media.githubusercontent.com` form). Same aggregation. VIINA is
  methodologically parallel to our own pipeline (news→NLP→geocode) — divergence from VIINA
  specifically flags *source-mix* gaps rather than extraction gaps.
- **Admin panel:** heat table of `our_events / benchmark` ratio by adm2×week with the worst cells
  highlighted; the actionable output is "enable more sources covering raion X".

## 11.9 Internet-outage signals (IODA / Cloudflare Radar) — adapter `net_outages`, kind `signal` — LAST, optional

Region-level connectivity drops corroborate strikes on power/telecom. IODA's API is alive (my
probe 400'd on parameters, not on the host); Cloudflare Radar needs a free token
(`CLOUDFLARE_RADAR_TOKEN`) and has clean `location=UA` outage endpoints. Emit
`Signal(kind='net_outage', region_code=…, active=…)` intervals into `region_status_intervals`.
Value is real but narrower than fires/alerts — schedule it last and cut it first if Phase 4 slips.

## 11.10 Explicitly rejected (record in the CSV notes, don't revisit silently)

- **X/Twitter accounts** (113 catalogued): API paywalled, Nitter dead. Reference entries only.
- **LiveUAMap:** commercial API — out of scope per README policy.
- **Raw Sentinel-1/2:** processing incompatible with the 2 vCPU / 4 GB budget.
- **ADS-B Exchange:** now paid; low relevance to a frontline map.
- **Oryx:** superseded by WarSpotting for machine-readable losses (WarSpotting `ukraine` side is a
  cheap flag-flip in the existing adapter if UA-side losses are wanted — one source row).

---

# Part IV — API and frontend

## 12. Public API additions

| Endpoint | Payload | Notes |
|---|---|---|
| `GET /api/signals?kind=fire&from=&to=&bbox=&near_frontline=1` | GeoJSON points, clustered above the existing threshold | `near_frontline` filters to `GREY_BUFFER + SENSOR_LAYER_KM` (default 15 km) of the current line — the default for the public layer; `near_frontline=0` is the "show all fires" toggle |
| `GET /api/alerts` | `{regions: [{code, name, active, since}], as_of}` | current air-alert state; ETagged, 30 s cache |
| `GET /api/alerts/history?region=&from=&to=` | intervals | drives scrubber shading |
| `GET /api/overlays/fortifications` | GeoJSON lines | static-ish, long cache |
| `/api/events` | + `corroborated=1` filter; event properties gain `corroboration` | §8.1 badge |
| `/api/config` | + layer registry: `{signals: [...], overlays: [...]}` with colours/glyphs | SPA builds layer toggles from config, not hardcode — **this is what makes the next layer a config-only change** |
| `/api/timeline` | + per-day fire counts and alert-hours (cheap aggregates) | scrubber context strip |

All follow the existing ETag/If-None-Match and blackout-zone regime; blackout zones apply to
`/api/signals` exactly as to events (publish-time filter only).

## 13. SPA changes (MapLibre)

- **Layer control** rendered from `/api/config` layer registry: fires (heat/point toggle by zoom),
  air alerts (oblast fill at low opacity, pulsing while active), civilian harm (✚ icons),
  fortifications (lines), each independently toggleable and persisted in `localStorage`.
- **Time scrubber:** fires and alert intervals are windowed by the same `occurred_at` semantics as
  events; alert shading appears in the scrubber track itself.
- **Popups:** sensor-corroboration badge on events (§8.1 wording); civilian-harm popups link the
  Bellingcat incident id; fire popups show satellite + FRP + acquisition time and the standard
  FIRMS attribution.
- **Legend/attribution:** NASA FIRMS, CIR Eyes on Russia, Bellingcat, UAControlMap, geoBoundaries
  join the attribution block (README + map footer).

---

# Part V — Operations

## 14. Node budget and cadences (reference 2 vCPU / 4 GB)

| source | interval | payload/day | rows/day | notes |
|---|---|---|---|---|
| firms ×3 satellites | 1800 s | ~1–3 MB CSV | 200–2000 | bulk queue not needed; CSV slices are small |
| air_alerts | 120 s | ~4 KB JSON | ~50–200 edges | negligible |
| eyes_on_russia | 3600 s | export diff | ~10–50 | size unknown until endpoint discovery — if the payload is a full multi-MB export, move to `bulk` queue + daily |
| bellingcat_civharm | 86400 s | ~5–20 MB | ~0–30 | `bulk` queue, conditional GET |
| uacontrolmap | 3600 s | ~1–5 MB KML | 1 layer | `bulk` |
| isw extra layers ×3 | 3600–21600 s | ~1 MB | few | existing pattern |
| html_list (mil.ru) + RU RSS | 900–1800 s | small | tens | LLM queue impact: expect +10–20% pending items; watch admin dashboard, raise `LLM_BATCH_SIZE` ceiling only if the proxy has headroom |
| acled_hdx / viina | weekly | 10–80 MB | aggregates only | `bulk`; runs Sunday night with `maintenance` |

Postgres growth: `sensor_signals` dominates (~≤2000 rows/day ≈ <1 GB/decade with indexes —
non-issue, but add `SIGNAL_RETENTION_DAYS` (default 0 = keep; the time scrubber wants history) and
a `maintenance` hook so the knob exists). New tables join the nightly `pg_dump` automatically.

## 15. Failure handling — nothing new to invent

Every new adapter inherits the existing regime by construction (it runs inside `poll_source`):
consecutive-failure degradation, 403-never-dead, markup-drift counters where applicable
(`html_list`, `uacontrolmap` set `empty_page` when a 200 parses to zero items/polygons), plausibility
bands on control layers, and per-provider degradation in `generation_meta` for the builder. Admin
dashboard additions: sensor-signal freshness (last `observed_at` per kind — a *stale sensor* is
information: FIRMS gap ⇒ cloud cover or outage; alert-feed gap ⇒ mirror down) and the pairwise
control-area disagreement panel (§9).

Secrets-missing is a first-class degraded state with an explicit message (§5), so a fresh deploy
without optional keys runs exactly like today, minus the keyed sources.

---

# Part VI — Phasing

Each phase is shippable and independently valuable; later phases never block on earlier *sources*,
only on Phase 0's interfaces.

**Phase 0 — adapter registry refactor (the enabler; no user-visible change).**
Protocol + registry + sinks + generic dispatch/preview/backfill + contract-test harness + source
row migration; delete `pull_*` tasks and their beat entries; collapse the duplicated
`_ingest_geolocated`. *Acceptance:* fixture-replay parity for all four API sources; beat schedule
reduced as §7; 189 existing tests green; `--source` backfill drives geoconfirmed/warspotting/telegram.

**Phase 1 — cheap wins on existing code.**
Extra ISW layers (3 source rows + centroid guard + `declared_grey` union generalization);
WarSpotting `ukraine` side row (if wanted); enable curated RU Telegram/RSS set (ops checklist in
the PR, not code). *Acceptance:* claims begin showing RU-perspective evidence links in admin review;
grey zone visibly consumes `isw_claimed`.

**Phase 2 — sensor infrastructure + the two flagship sensors.**
§8 tables + `seed_regions.py` + signal sink; `firms` and `air_alerts` adapters (**request
alerts.in.ua / ukrainealarm tokens at Phase-0 start**); `corroborate_events` job +
`meta_corroboration`; `/api/signals`, `/api/alerts*`; SPA layer registry + fire/alert layers +
badges. *Acceptance:* fires and alerts render and scrub through time; a synthetic fixture event
near a fixture fire gets the badge; rules-engine outputs are **bit-identical** with sensors present
(regression test that `_confirmation` ignores them).

**Phase 3 — geolocation redundancy + third line.**
`eyes_on_russia` (starts with endpoint discovery; timebox it — fall back to dataset mode);
`bellingcat_civharm` + `civilian_harm` event type; `uacontrolmap` + N-provider builder (§9) behind
`CONTROL_PROVIDERS`, unanimity only. *Acceptance:* a debunk retracts co-located proofs from both
verifiers (new test); builder n=2 output byte-identical to pre-refactor (regression); with the
third provider enabled, grey zone grows only (assert area monotonicity in the integration test).

**Phase 4 — calibration + optional outages.**
`benchmark_counts` + ACLED-HDX + VIINA weekly aggregation + admin coverage panel; `net_outages` if
time allows. *Acceptance:* admin heat table renders; ACLED raw rows provably never persisted
(test on the sink); public API surface unchanged by this phase.

Documentation lands **with each phase**, not at the end: VERIFIED_ENDPOINTS.md probe blocks, CSV
catalogue rows (`machine_readable` column updated), README source/attribution updates, and a
DEVIATIONS.md entry whenever an endpoint disagrees with this spec.

---

# Part VII — The cookbook this plan must make true

When this plan is done, `plans/ADDING_A_SOURCE.md` (write it in Phase 0, keep it honest) says:

1. Probe the upstream; append a block to `sources/VERIFIED_ENDPOINTS.md`.
2. Pick the output kind: text / geo_events / control / signal. If none fits, stop — that's an
   architecture conversation, not an adapter.
3. Write `celery_worker/adapters/<name>.py`: `name`, `kind`, `default_interval_s`,
   `poll(source, state)`; optionally `backfill`. Import it in `adapters/__init__.py`.
4. Drop a captured response into `tests/fixtures/adapters/<name>/`; the contract suite picks it up
   automatically. Add edge-case tests for the parsing quirks you found in step 1.
5. Add the row: CSV catalogue entry + `seed_sources.py` inclusion (or admin UI), with `perspective`,
   `reliability_tier`, `poll_interval_s`, `meta.adapter`, and `meta.secret_env` if keyed.
6. If it needs a key: entry in `.env.example`, nothing in the DB.
7. Enable it in admin, hit *test fetch*, watch one poll cycle on the dashboard.

Steps 3–4 are the only code. If any future source needs more than this, the deviation goes in
DEVIATIONS.md with the reason.

---

# §16 Appendix

## 16.1 Endpoint probe log (2026-08-24, from a residential/dev IP)

```
ubilling.net.ua/aerialalerts/                    200 application/json  ~4 KB   live per-oblast alertnow+changed; key-free
eyesonrussia.org/events.json                     200 text/html         SPA shell — real endpoint needs DevTools discovery
eyesonrussia.org/api/events                      200 text/html         same
map.eyesonrussia.org/events.json                 525                   TLS/origin error
ukraine.bellingcat.com/ukraine-server/api/...    404                   old TimeMap API gone; use published dataset export
github API zhukovyuri/VIINA/contents/Data        200                   yearly zips present; 130-byte entries ⇒ Git-LFS pointers
api.ioda.inetintel.cc.gatech.edu (…summary…)     400                   host alive; parameters need reading their docs
data.humdata.org package_show ukraine-acled…     200                   HDX ACLED mirror package resolvable
```

(Older probes — DeepState, ISW ArcGIS 324-service enumeration, GeoConfirmed OpenAPI, WarSpotting,
OSW RSS, no-ISW-RSS, alerts.in.ua/ukrainealarm auth walls, NZZ territory.csv — remain valid in
`sources/VERIFIED_ENDPOINTS.md` as of 2026-08-19.)

## 16.2 New environment variables

```
FIRMS_MAP_KEY=            # https://firms.modaps.eosdis.nasa.gov/api/map_key/ (free, instant)
ALERTS_IN_UA_TOKEN=       # request at alerts.in.ua (free, non-commercial)
UKRAINEALARM_KEY=         # request at api.ukrainealarm.com (fallback alert source)
CLOUDFLARE_RADAR_TOKEN=   # optional, Phase 4 outages
FIRMS_BBOX=               # optional override, defaults to ukraine_bbox extended east to 42.5
CONTROL_PROVIDERS=        # JSON, default {"deepstate":{},"isw":{}}
AGREEMENT_QUORUM=0        # 0=unanimity; 2 enables 2-of-3 once uacontrolmap has a track record
SENSOR_CORROBORATION_KM=5
SENSOR_LAYER_KM=15
SIGNAL_RETENTION_DAYS=0   # 0 = keep forever
```

## 16.3 Licensing / redistribution matrix

| source | license posture | public map? |
|---|---|---|
| NASA FIRMS | free, cite LANCE/FIRMS | yes, with attribution |
| air alerts (ubilling/alerts.in.ua/ukrainealarm) | free / token ToS: verify redistribution clause when token granted | yes (state, not their raw feed) |
| Eyes on Russia | verify CIR terms during endpoint discovery | expected yes w/ attribution; confirm |
| Bellingcat civharm | verify current dataset license | yes if permitted, else admin-only |
| UAControlMap | community map; ask author, attribute regardless | yes w/ attribution |
| ISW ArcGIS extra layers | same terms as current usage | yes (already attributed) |
| ACLED (HDX mirror) | **no redistribution** | **never** — admin aggregates only |
| VIINA | academic, citation | admin aggregates only (consistent posture) |
| geoBoundaries ADM1/2 | open (CC BY) | yes, attribution |

## 16.4 Open questions (resolve during the named phase)

1. *(Phase 2)* alerts.in.ua token ToS: may historical alert intervals be shown publicly, or
   state-only? Determines whether `/api/alerts/history` is public or admin.
2. *(Phase 3)* Eyes on Russia endpoint shape and stability — dataset-mode fallback decision point
   after a timeboxed week.
3. *(Phase 3)* UAControlMap folder taxonomy and author contact for blessing + MID stability.
4. *(Phase 3, later)* Whether 2-of-3 quorum ever becomes defensible — requires the §9 disagreement
   panel to show sustained three-way agreement first.
5. *(Phase 2, data-driven, explicitly deferred)* Whether sensor corroboration should ever gain rule
   weight. Default answer is no (§8.1); revisit only with measured precision numbers.
