# Ukraine Frontline Aggregator — Full Implementation Specification

This document is a **self-contained build specification**. An implementing agent needs only this
file plus two data files already in the repo:

- `sources/ukraine_frontline_sources.csv` — 538 catalogued news/OSINT/data sources (schema in §9)
- `sources/VERIFIED_ENDPOINTS.md` — probe logs for the external APIs used below (background/reference)

Everything else — architecture, schemas, algorithms, API contracts, prompts, configuration,
acceptance criteria — is specified here. Where this spec and reality diverge (an upstream API
changed), prefer reality, log the divergence in `plans/DEVIATIONS.md`, and keep the interface
contracts of §15 stable.

---

## 1. Mission

Build a deployable web service showing a live map of Ukraine with:

1. **Frontline + grey zone display** — a rendered frontline with an explicit "grey zone" layer for
   territory whose control is unclear.
2. **Geolocated news events** — news from many sources (RSS, Telegram, APIs) run through an LLM
   (via a LiteLLM proxy) to extract locations; events appear as clickable icons on the map AND in a
   separate news-feed tab. Every item is visibly labelled with its perspective
   (`ukrainian` / `russian` / `western` / `neutral`) and its source site/channel name. Both
   free-text place names and explicit coordinates must be handled.
3. **Evidence-based frontline adjustment**:
   - 3.1 a frontline change is confirmed when sources from **≥ 2 distinct perspective classes**
     report it, OR
   - 3.2 a single **geolocation proof** (verified coordinates/imagery) confirms it alone. Because
     geolocations can be AI-faked, later "debunk" reports must retract prior proofs and **revert**
     any frontline change that loses its evidence.
   - 3.3 **deep strikes** (drones/missiles far behind the lines) get map icons but never move the
     frontline or grey zone.
4. **Admin backend** — edit/delete/add notifications and sources (Telegram, RSS, …), and draw
   **blackout zones**: polygons inside Ukraine where news updates are hidden from the public site.

Core principles: **scalability** (stateless web tier, queue workers, can be replicated later) and
**compute efficiency** (everything fits one 2 vCPU / 4 GB node; LLM inference is off-node via the
LiteLLM proxy).

## 2. Current repository state

Flask + Celery + Redis starter template:

```
app/__init__.py        # Flask app + make_celery(); config hardcodes redis://redis:6379/0
app/routes.py          # demo routes: /, /run-task, /wait/<task_id>
celery_worker/tasks.py # demo task; its own Celery instance
Dockerfile.web         # gunicorn container
Dockerfile.worker      # celery worker container
docker-compose.yml     # web (port 5001), worker, redis
requirements.txt       # flask, celery, redis, gunicorn
deploy.sh              # docker-compose up --build wrapper
.env                   # exists locally, gitignored — see §4
sources/               # the two data files named above
plans/PLAN.md          # this spec
```

The demo routes/tasks may be deleted. Keep the web/worker container split.

## 3. Architecture

```
                          ┌─────────────────────────────────────────────┐
  browsers ── nginx ──►   │ web (Flask/gunicorn, 2 workers, stateless)  │
                          │   /            MapLibre SPA (static files)  │
                          │   /api/*       GeoJSON + news JSON (cached) │
                          │   /admin/*     session-auth admin           │
                          └───────┬─────────────────────────────────────┘
                                  │
        ┌────────── Redis ────────┼───────────── Postgres + PostGIS ─────────┐
        │  broker + result +      │   sources, news_items, events, claims,   │
        │  GeoJSON response cache │   snapshots, gazetteer, blackouts, …     │
        └──────────┬──────────────┘  └──────────────────────────────────────┘
                   │
   ┌───────────────┴───────────────┐
   │ celery beat (scheduler)       │
   │ celery worker (concurrency 2) │──► LiteLLM proxy (external)
   │  pollers → llm_extract →      │
   │  geocode → rule engine →      │
   │  frontline builder            │
   └───────────────────────────────┘
```

Non-negotiable decisions (rationale in comments):

- **PostGIS** (postgis/postgis:16 image) — geometric difference for the grey zone, bbox queries,
  trigram gazetteer matching are single SQL calls.
- **MapLibre GL JS** frontend as **static files** (no Node at runtime; vendor the JS/CSS or use CDN
  with local fallback). Raster base tiles from OSM tile servers with proper attribution.
- **No websockets.** Frontend polls; responses are Redis-cached with ETags.
- **All LLM calls via LiteLLM proxy** using the OpenAI-compatible chat-completions API.
- **Perspective labels come from the source record, never from the LLM** (deterministic, auditable).
- **All geocoding is local** against a gazetteer table. No external geocoding API.

## 4. Configuration

`.env` already exists (gitignored) with exactly these keys; ship `.env.example` with placeholders:

| Variable | Meaning |
|---|---|
| `LITELLM_MODEL` | model name passed as `model` in chat-completion calls |
| `LITELLM_API_KEY` | bearer key for the proxy |
| `LITELLM_API_BASE` | base URL of the LiteLLM proxy (OpenAI-compatible; call `{base}/chat/completions`) |

Add (with defaults in code so the stack boots without them):

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://ukraine:ukraine@postgis:5432/ukraine` | SQLAlchemy DSN |
| `REDIS_URL` | `redis://redis:6379/0` | broker/result/cache |
| `SECRET_KEY` | random-at-boot (warn) | Flask sessions |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | none → admin disabled | initial admin login (password bcrypt-hashed into DB at first boot) |
| `PUBLIC_POLL_SECONDS` | `90` | frontend refresh hint served at `/api/config` |
| `HTTP_USER_AGENT` | `UkraineAggregator/1.0 (+contact-url)` | all outbound polling |

Application constants live in `app/config.py` as a dataclass (all tunable via env override):

| Constant | Default | Used by |
|---|---|---|
| `DEEP_STRIKE_KM` | 30 | rule 3.3: distance behind the line beyond which an event is a deep strike |
| `CLAIM_JOIN_KM` | 5 | events within this distance of an existing claim join it |
| `GREY_BUFFER_KM` | 3 | buffer around unresolved claims added to the grey zone |
| `GAZETTEER_MIN_SIMILARITY` | 0.55 | pg_trgm similarity threshold to accept a place match |
| `LLM_BATCH_SIZE` | 8 | news items per extraction call |
| `LLM_MAX_RETRIES` | 3 | with exponential backoff on 429/5xx |
| `LLM_MAX_BODY_CHARS` | 4000 | truncate item text before sending |
| `POLL_JITTER_S` | 0–30 | random stagger added to every poll task |
| `SNAPSHOT_SIMPLIFY_TOLERANCES` | `{low:0.01, mid:0.002, high:0.0005}` | degrees, per zoom tier |

## 5. Target file tree

```
app/
  __init__.py            # app factory; init db, redis, celery, blueprints, login manager
  config.py              # env + constants (§4)
  extensions.py          # db (SQLAlchemy), redis client, celery, login_manager singletons
  models/                # one module per table group (§7)
      __init__.py sources.py news.py events.py frontline.py gazetteer.py admin.py
  api/                   # public API blueprint (§15)
      __init__.py frontline.py events.py news.py meta.py
  admin/                 # admin blueprint (§17): auth.py, views.py, forms
  services/
      cache.py           # redis get/set with ETag helpers
      geo.py             # simplify, bbox parse, blackout filter SQL fragments
      llm.py             # LiteLLM client wrapper (§11)
      geocode.py         # gazetteer matching (§12)
      rules.py           # claim state machine (§13)
      frontline.py       # snapshot builder (§14)
  static/                # SPA: index.html, app.js, style.css, vendored maplibre, icons/
celery_worker/
  celery_app.py          # single Celery instance, beat schedule (§10)
  tasks/
      poll.py            # dispatch_polls, poll_source (rss/telegram/api adapters)
      extract.py         # llm_extract_batch
      geocode.py         # geocode_pending
      rules.py           # evaluate_claims
      frontline.py       # rebuild_frontline
      maintenance.py     # cleanup, snapshot pruning, health rollup
  adapters/
      rss.py telegram_web.py deepstate.py isw_arcgis.py geoconfirmed.py warspotting.py
migrations/              # alembic
scripts/
  seed_sources.py        # load sources/ukraine_frontline_sources.csv (§9)
  seed_gazetteer.py      # build gazetteer (§8.5)
  create_admin.py
nginx/nginx.conf
Dockerfile.web Dockerfile.worker docker-compose.yml
requirements.txt
tests/                   # pytest (§20)
```

`requirements.txt` (pin at implementation time): flask, gunicorn, celery, redis,
SQLAlchemy>=2, GeoAlchemy2, psycopg[binary], alembic, feedparser, httpx, beautifulsoup4, lxml,
shapely, flask-login, flask-wtf, bcrypt, python-dotenv, orjson, tenacity, pytest.

## 6. Infrastructure

`docker-compose.yml` services:

- `nginx` — ports 80:80; serves `app/static/` directly, proxies `/api` and `/admin` to `web`;
  `gzip on` for json/geojson; `proxy_cache` off (Redis handles caching); healthcheck: `wget -qO- localhost/healthz`.
- `web` — gunicorn `-w 2 -k gthread --threads 4 -b 0.0.0.0:5001 "app:create_app()"`.
- `worker` — `celery -A celery_worker.celery_app worker --concurrency=2 -Q default,llm`.
- `beat` — `celery -A celery_worker.celery_app beat`.
- `redis` — `redis:7-alpine`, `--maxmemory 128mb --maxmemory-policy allkeys-lru`.
- `postgis` — `postgis/postgis:16-3.4-alpine`; volume `pgdata`; env from compose;
  `shared_buffers=256MB`, `work_mem=16MB`.
- All app containers read `.env` via `env_file`. Add restart policies `unless-stopped`.

RAM budget ≈ 1.3 GB total; must fit 2 vCPU / 4 GB.

## 7. Database schema

Alembic-managed. Extensions: `postgis`, `pg_trgm`. All geometry SRID 4326. All timestamps UTC.

```sql
-- sources: one row per ingestible feed/channel/API
CREATE TABLE sources (
  id            SERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  type          TEXT NOT NULL CHECK (type IN ('rss','telegram','api','scrape')),
  url           TEXT NOT NULL,            -- feed URL, t.me/s/<slug> URL, or API base
  perspective   TEXT NOT NULL CHECK (perspective IN ('ukrainian','russian','western','neutral')),
  reliability_tier SMALLINT NOT NULL DEFAULT 2,   -- 1 high, 2 normal, 3 low (3 can never confirm alone)
  poll_interval_s  INTEGER NOT NULL DEFAULT 900,
  enabled       BOOLEAN NOT NULL DEFAULT true,
  last_polled_at TIMESTAMPTZ, last_success_at TIMESTAMPTZ,
  etag TEXT, last_modified TEXT,          -- HTTP conditional-GET state
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','degraded','dead')),
  meta          JSONB NOT NULL DEFAULT '{}'       -- adapter-specific (e.g. telegram slug)
);

CREATE TABLE news_items (
  id            BIGSERIAL PRIMARY KEY,
  source_id     INTEGER NOT NULL REFERENCES sources(id),
  external_id   TEXT,                      -- guid / t.me message id
  content_hash  TEXT NOT NULL,             -- sha256(title+body normalized); UNIQUE
  title         TEXT, body TEXT, url TEXT,
  published_at  TIMESTAMPTZ, fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  llm_status    TEXT NOT NULL DEFAULT 'pending'
                CHECK (llm_status IN ('pending','done','skipped','failed')),
  UNIQUE (content_hash)
);
CREATE INDEX ON news_items (llm_status) WHERE llm_status = 'pending';
CREATE INDEX ON news_items (published_at DESC);

CREATE TABLE extracted_events (
  id            BIGSERIAL PRIMARY KEY,
  news_item_id  BIGINT NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
  event_type    TEXT NOT NULL CHECK (event_type IN
                ('frontline_advance','frontline_claim','deep_strike','shelling',
                 'geolocation_proof','debunk','other')),
  geom          geometry(Point,4326),      -- NULL until geocoded; NULL = feed-only item
  place_name_raw TEXT,
  gazetteer_id  INTEGER REFERENCES gazetteer(id),
  coord_source  TEXT CHECK (coord_source IN ('explicit_coords','gazetteer_match')),
  claimed_by    TEXT CHECK (claimed_by IN ('ru','ua',NULL)),
  confidence    REAL,                      -- LLM self-reported 0..1
  llm_raw       JSONB,
  visible       BOOLEAN NOT NULL DEFAULT true,   -- admin hide switch
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON extracted_events USING GIST (geom);
CREATE INDEX ON extracted_events (event_type, created_at DESC);

CREATE TABLE gazetteer (
  id SERIAL PRIMARY KEY,
  katottg TEXT,                            -- Ukrainian admin codifier code when known
  name_uk TEXT NOT NULL, name_ru TEXT, name_en TEXT,
  name_search TEXT NOT NULL,               -- lower, transliterated, concatenated variants
  oblast TEXT, raion TEXT,
  geom geometry(Point,4326) NOT NULL,
  boundary geometry(MultiPolygon,4326)     -- settlement polygon when available
);
CREATE INDEX ON gazetteer USING GIN (name_search gin_trgm_ops);
CREATE INDEX ON gazetteer USING GIST (geom);

CREATE TABLE frontline_claims (
  id BIGSERIAL PRIMARY KEY,
  geom geometry(Point,4326) NOT NULL,      -- claim centroid (settlement point)
  gazetteer_id INTEGER REFERENCES gazetteer(id),
  direction TEXT NOT NULL CHECK (direction IN ('ru_advance','ua_advance')),
  status TEXT NOT NULL DEFAULT 'pending'
         CHECK (status IN ('pending','confirmed','rejected','reverted')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ,
  resolved_by TEXT                          -- 'rule:corroboration'|'rule:geoproof'|'admin:<user>'
);
CREATE INDEX ON frontline_claims USING GIST (geom);

CREATE TABLE evidence_links (
  id BIGSERIAL PRIMARY KEY,
  claim_id BIGINT NOT NULL REFERENCES frontline_claims(id) ON DELETE CASCADE,
  event_id BIGINT NOT NULL REFERENCES extracted_events(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('support','geolocation_proof','debunk')),
  active BOOLEAN NOT NULL DEFAULT true,     -- flipped false when debunked
  UNIQUE (claim_id, event_id)
);

CREATE TABLE frontline_snapshots (
  id BIGSERIAL PRIMARY KEY,
  built_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  layer TEXT NOT NULL CHECK (layer IN ('ru','grey')),   -- 'ua' = everything else; don't store it
  geom geometry(MultiPolygon,4326) NOT NULL,
  simplified JSONB NOT NULL,               -- {low:geojson, mid:geojson, high:geojson}
  generation_meta JSONB NOT NULL           -- {deepstate_id, isw_editdate, applied_claim_ids: []}
);

CREATE TABLE blackout_zones (
  id SERIAL PRIMARY KEY,
  name TEXT, reason TEXT,
  geom geometry(Polygon,4326) NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), created_by TEXT
);

CREATE TABLE notifications (
  id SERIAL PRIMARY KEY,
  title TEXT NOT NULL, body TEXT,
  level TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info','warning','alert')),
  active BOOLEAN NOT NULL DEFAULT true,
  starts_at TIMESTAMPTZ, ends_at TIMESTAMPTZ
);

CREATE TABLE admin_users (
  id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL
);

CREATE TABLE audit_log (
  id BIGSERIAL PRIMARY KEY,
  at TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT, entity_id TEXT, detail JSONB
);
```

## 8. External data contracts (verified 2026-08; treat as ground truth until they 404)

### 8.1 DeepStateMap — Ukrainian-OSINT frontline polygons (no auth)
- `GET https://deepstatemap.live/api/history/public` → JSON array (~1,700+ records):
  `{id, description (uk, may embed <a> links), descriptionEn, updatedAt, datetime, status, createdAt}`
- `GET https://deepstatemap.live/api/history/last` → `{id, map: <GeoJSON FeatureCollection>}`
  (~0.6 MB). Polygons are **occupied/Russian-controlled areas**; coordinates are `[lon, lat, 0]`
  triples — strip the z. Features include areas outside Ukraine context; keep only polygons.
- `GET https://deepstatemap.live/api/history/{id}/geojson` → FeatureCollection for a past snapshot.
- Known traps: `/api/history` alone → 401; `/api/history/last/geojson` → 404.
- Poll every 30 min. Store latest id in `sources.meta`; skip if unchanged.

### 8.2 ISW / Critical Threats ArcGIS — Western assessed control (no auth)
- Base: `https://services5.arcgis.com/SaBe5HMtmnbqSWlu/arcgis/rest/services`
- `GET {base}?f=json` enumerates ~324 services (multiple conflicts; names change without notice —
  re-resolve by name pattern on failure).
- Query pattern: `{base}/{service}/FeatureServer/{layer}/query?where=1%3D1&outFields=*&returnGeometry=true&f=geojson`
  Supports `resultRecordCount`/`resultOffset` paging.
- Verified layers:
  - `VIEW_RussiaCoTinUkraine_V3/FeatureServer/49` → Polygon, assessed Russian control; has
    `EditDate` epoch-ms field → poll cheaply by comparing `EditDate` (query with
    `outFields=EditDate&returnGeometry=false` first).
  - `Assessed_Russian_Gains_in_the_Past_24_Hours_view/FeatureServer/0` → Polygon.
  - `RUAF_Field_Fortifications_Polylines/FeatureServer/0` → LineString (optional overlay).
- Poll every 60 min.

### 8.3 GeoConfirmed — volunteer-verified geolocations = `geolocation_proof` feed (no auth)
- OpenAPI spec: `https://geoconfirmed.org/openapi/v1.json` (NOT /swagger/).
- **Trap:** `GET /api/placemark/Ukraine` (lowercase) returns an icon taxonomy, not data.
- Use: `GET https://geoconfirmed.org/api/Map/export/Ukraine/csv` → ~31 MB, **semicolon-delimited,
  UTF-8 BOM**, ~59k rows, columns:
  `Date;Name;Faction;Origin;Latitude;Longitude;PlusCode;Description;Source;Geolocation;Equipment;EquipmentItems;Units;OrbatUnits;Id`
- Or JSON: `GET /api/Placemark/Ukraine/geojson` → `{factionMeta, geojson:{FeatureCollection}}`,
  properties only `{id, icon, factionId, color, date, dateSort}`; per-event detail at
  `GET /api/Placemark/detail/{id}`.
- Ingest strategy: daily full CSV pull, upsert by `Id` into `extracted_events`
  (`event_type='geolocation_proof'`, synthetic `news_items` row per record, source = the
  GeoConfirmed source row, `coord_source='explicit_coords'`). Only ingest rows dated within the
  last N days after the initial backfill.

### 8.4 WarSpotting — geolocated equipment losses (no auth; secondary corroboration)
- `GET https://ukr.warspotting.net/api/losses/russia/recent/` → `{"losses":[...]}`, 100 items,
  fields `{id,type,model,status,lost_by,date,nearest_location,geo:"lat,lon",unit,tags}`.
- `GET /api/losses/russia/<YYYY-MM-DD>/` per-date (backfills late — poll `recent` AND re-poll the
  last 7 dates daily). Ingest as `event_type='other'` support evidence.

### 8.5 Gazetteer seeding (`scripts/seed_gazetteer.py`)
Primary: download the KATOTTG codifier (Ukrainian Ministry of Communities; CSV/XLSX published on
data.gov.ua — search "КАТОТТГ") for the authoritative settlement list, then join coordinates from
OSM (Overpass query: `node["place"~"city|town|village|hamlet"](area:Ukraine)` — chunk by oblast to
respect Overpass limits) matching on normalized names + oblast. Where the ISW service list contains
a Ukrainian settlements layer (enumerate §8.2 base, look for names containing `Settlement`), prefer
its polygons for `boundary`. Store every name variant (uk, ru, en transliteration via a simple
translit table) concatenated into `name_search`. Target: ≥ 25k settlements. This script runs once
at setup and is idempotent (upsert by katottg, else by (name_uk, oblast)).

### 8.6 Generic RSS and Telegram
- RSS: `feedparser`; send `If-None-Match`/`If-Modified-Since` from `sources.etag/last_modified`;
  update them from responses; 304 → done.
- Telegram: fetch `https://t.me/s/<slug>` (slug in `sources.meta.slug`), parse with BeautifulSoup:
  message blocks `.tgme_widget_message`, text `.tgme_widget_message_text`, time
  `.tgme_widget_message_date time[datetime]`, id from `data-post`. Keep messages newer than the
  last stored `external_id`. Some channels disable previews → adapter returns `[]` and after 5
  consecutive empty+changed-markup results marks the source `degraded`.
- ~15 catalogued sites 403 datacenter IPs (flagged in the CSV `reliability_notes`). Poller treats
  403 as `degraded`, never dead, and never retries more than once per interval.

## 9. Source catalogue seeding (`scripts/seed_sources.py`)

`sources/ukraine_frontline_sources.csv` columns (quoted CSV, header row):
`title,url,category,source_type,affiliation_perspective,language,geo_granularity,update_frequency,access,machine_readable,primary_platform,handle_or_feed,reliability_notes,description,last_checked`

Seeding rules — do NOT import all 538 rows as pollable sources; import a curated working set:
1. Rows where `machine_readable` contains `RSS` and `handle_or_feed` holds a feed URL → `type='rss'`.
2. Rows with `primary_platform='Telegram'` and a `t.me` URL → `type='telegram'`,
   `meta.slug` parsed from the URL. Skip rows whose reliability notes say preview-disabled/dormant.
3. The four §8 APIs → `type='api'`, adapter name in `meta.adapter`.
4. Map `affiliation_perspective`: `Ukrainian→ukrainian`, `Russian→russian`, `Western→western`,
   `International/Neutral→neutral`.
5. `reliability_tier`: 1 for Government/Think Tank/OSINT-verification rows, 2 default, 3 where
   notes mention propaganda/low verification. Everything imported starts `enabled=false` except a
   starter set (~25 sources balanced across perspectives) listed in the seed script; the admin
   enables more from the UI.
6. Poll intervals by `update_frequency`: Real-time→300s, Multiple daily→900s, Daily→3600s, else 7200s.

## 10. Celery wiring

Single Celery instance in `celery_worker/celery_app.py` (fix the template's duplicate-instance
smell; the Flask app imports it from `app/extensions.py`). Queues: `default`, `llm`.

Beat schedule:

| Task | Every | Queue |
|---|---|---|
| `dispatch_polls` (fan out `poll_source(id)` for due, enabled sources, with jitter) | 60 s | default |
| `llm_extract_batch` (if pending items exist) | 120 s | llm |
| `geocode_pending` | 120 s | default |
| `evaluate_claims` | 300 s | default |
| `rebuild_frontline` (only if dirty flag set in Redis, else skip) | 300 s | default |
| `rebuild_frontline(force=True)` nightly 03:30 UTC | daily | default |
| `pull_geoconfirmed` daily 04:00, `pull_warspotting` hourly, `pull_deepstate` 30 min, `pull_isw` 60 min | — | default |
| `maintenance` (prune snapshots > 90 days keeping 1/day, health rollup) | daily | default |

Failure policy: `tenacity` retry inside adapters (3 tries, expo backoff); on final failure increment
`sources.consecutive_failures`; ≥ 5 → `status='degraded'`; ≥ 20 → `'dead'` + skip until admin reset.

## 11. LLM extraction (`app/services/llm.py`, task `llm_extract_batch`)

- Client: `httpx` POST `{LITELLM_API_BASE}/chat/completions`, header
  `Authorization: Bearer {LITELLM_API_KEY}`, body `{model: LITELLM_MODEL, messages, temperature: 0,
  response_format: {type:'json_object'}}` (if the proxy rejects `response_format`, fall back to
  prompt-enforced JSON and parse defensively).
- **Prefilter before spending tokens** (in `llm_extract_batch`): mark `llm_status='skipped'` unless
  the text matches a war-relevance regex (uk/ru/en keyword classes: strike/удар/обстріл/наступ/
  просунул/звільни/захопи/дрон/ракет/КАБ/frontline/advance/liberate/captured/shelling/missile/
  drone + oblast/settlement suffix heuristics). Keep the regex list in `config.py`.
- Batch: up to `LLM_BATCH_SIZE` items; each truncated to `LLM_MAX_BODY_CHARS`.
- System prompt (verbatim, keep stable for cache hits):

```
You extract structured battlefield facts from news items about the war in Ukraine.
For EACH input item return one JSON object. Respond with {"items": [...]} only, no prose.
Per item fields:
- "idx": the input index (integer, echo it back)
- "relevant": bool — is this about a concrete military event/claim in or near Ukraine?
- "event_type": one of "frontline_advance","frontline_claim","deep_strike","shelling",
  "geolocation_proof","debunk","other"
   * frontline_advance: a side reportedly took/entered/liberated a specific settlement
   * frontline_claim: fighting for / assault on a named settlement without a control change
   * deep_strike: drone/missile strike far from the frontline (rear areas, cities, refineries)
   * geolocation_proof: the item itself presents verified imagery/coordinates proving a position
   * debunk: the item argues that earlier footage/geolocation was fake, staged or AI-generated
- "locations": array of {"name": string or null, "lat": number or null, "lon": number or null,
   "oblast": string or null} — extract EVERY named settlement; parse coordinates if literally
   present in the text; NEVER invent coordinates for a name.
- "claimed_by": "ru","ua" or null — which side the reported gain/position favors
- "debunk_target": string or null — for debunk items: the settlement/claim/URL being disputed
- "confidence": 0..1 — your confidence in the extraction (not in the claim's truth)
```

- User message: JSON array `[{idx, source_name, published_at, title, body}, ...]`.
- Postprocess: for each returned item create `extracted_events` rows (one per location; a
  no-location relevant item gets one row with `geom=NULL`). Regex-scan the original text for
  coordinate patterns (`\d{1,2}\.\d{3,}[, ]\s*\d{1,2}\.\d{3,}`, DMS forms) — regex hits override
  LLM lat/lon. Sanity-gate coordinates to bbox `lat 43..53.6, lon 21..41.4` (Ukraine + border
  regions); outside → treat as name-only. Set `llm_status='done'` (or `'failed'` after retries;
  failed items retry max twice more on later batches).
- Cost controls: never send an item twice (status machine), batch, temperature 0, short outputs.

## 12. Geocoding (`app/services/geocode.py`, task `geocode_pending`)

For events with `geom IS NULL AND place_name_raw IS NOT NULL`:
1. Normalize: lowercase, strip punctuation, transliterate cyrillic→latin (single static table,
   both uk and ru schemes) → `q`.
2. `SELECT id, similarity(name_search, :q) s FROM gazetteer WHERE name_search % :q ORDER BY s DESC LIMIT 5`.
3. Disambiguate: if the LLM supplied `oblast`, filter candidates to it (fuzzy). If several
   candidates remain within 0.05 similarity of each other in different oblasts and no oblast
   context → leave unplaced (feed-only), log `ambiguous`.
4. Accept if best `s >= GAZETTEER_MIN_SIMILARITY`: set `geom = gazetteer.geom`,
   `gazetteer_id`, `coord_source='gazetteer_match'`. Else leave unplaced.
Never call an external geocoder. This is the hallucination firewall: **no gazetteer match and no
explicit coordinates ⇒ no map placement.**

## 13. Rule engine (`app/services/rules.py`, task `evaluate_claims`)

Definitions: `current_ru` = latest `frontline_snapshots.layer='ru'` geometry;
`dist_to_line(geom)` = `ST_Distance(geography)` to `ST_Boundary(current_ru)` in km.

**Intake** — for each new geocoded event not yet linked:
- `deep_strike`, or any event with `dist_to_line > DEEP_STRIKE_KM`: never touches claims (rule 3.3);
  it stays a map icon. (Exception: `debunk` events always enter matching, regardless of distance.)
- `frontline_advance` / `frontline_claim` with `claimed_by` and `dist_to_line <= DEEP_STRIKE_KM`:
  find an open claim (`status='pending'`) within `CLAIM_JOIN_KM` km with the same `direction`
  (`ru_advance` iff `claimed_by='ru'`); join it (`role='support'`) or open a new claim at the
  event's settlement point.
- `geolocation_proof` within `CLAIM_JOIN_KM` of a claim: link with `role='geolocation_proof'`.
  A proof with no nearby claim opens one (direction from `claimed_by`; if null, from which side of
  `current_ru` the point falls: inside → `ru_advance` evidence context, outside → `ua_advance`).
- `debunk`: match to prior `geolocation_proof` evidence by (a) URL mentioned in `debunk_target`
  matching the proof's news_item URL, else (b) same gazetteer settlement (or within 10 km) with the
  proof at most 30 days older. On match: set that evidence link `active=false`, audit-log it, and
  re-evaluate the claim (below). Unmatched debunks remain feed/map items.

**Evaluation** — for each `pending` claim:
- Corroboration set = active `support`+`geolocation_proof` links, resolved to their source
  perspectives, **excluding** `reliability_tier=3` sources.
- Confirm (`resolved_by='rule:corroboration'`) if the set spans **≥ 2 distinct perspectives** of
  {ukrainian, russian, western} (neutral counts as western for this rule — document this in code).
- Confirm (`resolved_by='rule:geoproof'`) if ≥ 1 active `geolocation_proof` link exists.
- A claim pending > 14 days with no new evidence → `rejected` (kept for audit).

**Re-evaluation after debunk** — for `confirmed` claims: recompute the same conditions over
*active* links only; if neither holds anymore → `status='reverted'`, `resolved_by='rule:debunk'`,
set the frontline dirty flag. Reverted claims can return to `confirmed` if new active evidence
arrives (state machine: pending → confirmed ⇄ reverted, pending → rejected).

Any state change sets Redis key `frontline:dirty=1`.

## 14. Frontline builder (`app/services/frontline.py`, task `rebuild_frontline`)

Runs when `frontline:dirty` is set, or nightly forced.

1. **Baseline**: `DS` = latest DeepStateMap occupied MultiPolygon (union of its polygons, z
   stripped, `ST_MakeValid`); `ISW` = latest ISW assessed-control MultiPolygon. If one upstream is
   stale/unavailable, use the other alone and record it in `generation_meta`.
2. **Grey zone** = `ST_SymDifference(DS, ISW)` — where the Ukrainian-OSINT and Western views
   disagree — unioned with `ST_Buffer(geography(claim.geom), GREY_BUFFER_KM km)` for every
   `pending` claim.
3. **RU layer** = `ST_Intersection(DS, ISW)` (agreed control), then apply resolved claims *newer
   than both baselines' timestamps* (usually none — baselines normally absorb confirmed changes
   within a day; claims are the fast path):
   - confirmed `ru_advance`: union the claim's settlement `boundary` (or 2 km point buffer) into RU
     and remove it from grey;
   - confirmed `ua_advance`: subtract likewise;
   - `reverted` claims: ensure their geometry contribution is absent (recompute from scratch each
     build — never mutate incrementally — so reverts are automatic).
4. Simplify each layer at the three `SNAPSHOT_SIMPLIFY_TOLERANCES` with
   `ST_SimplifyPreserveTopology`; store GeoJSON strings in `simplified`.
5. Insert `frontline_snapshots` rows (`ru`, `grey`), delete Redis keys `api:frontline:*`, clear
   dirty flag. Log build duration; target < 30 s on the reference node.

## 15. Public API (blueprint `app/api`, all responses `orjson`, ETag from a content hash,
Redis-cached until invalidated; 400 on malformed params)

- `GET /api/config` → `{poll_seconds, map_defaults:{center:[31.0,48.5], zoom:6}}`
- `GET /api/frontline?detail=low|mid|high` (default mid) →
  `{built_at, layers: {ru: <GeoJSON MultiPolygon>, grey: <GeoJSON MultiPolygon>}, meta:{...}}`
- `GET /api/events?bbox=minLon,minLat,maxLon,maxLat&from=ISO&to=ISO&types=a,b&perspectives=a,b&limit=`
  → GeoJSON FeatureCollection; feature properties:
  `{id, event_type, perspective, source_name, title, url, published_at, confidence, claim_id}`.
  Server-side: if matching rows > 500, cluster via `ST_SnapToGrid` (grid by zoom hint) and return
  cluster features `{cluster:true, count, expansion_bbox}`. Default window: last 72 h.
  **Blackout filter applied here** (§17).
- `GET /api/news?cursor=<id>&perspective=&source_id=&q=&limit=50` → newest-first
  `{items:[{id,title,url,published_at,source:{name,perspective},events:[{event_type,placed:bool,lat,lon}]}], next_cursor}`.
  Blackout filter: items whose *only* events lie inside active blackout zones are omitted;
  unplaced items always pass.
- `GET /api/notifications` → active rows within their time window.
- `GET /healthz` → `{db:ok, redis:ok, last_frontline_build, pending_llm, degraded_sources}` (also
  used by container healthchecks; no auth, no secrets).

## 16. Frontend (`app/static/` — index.html + app.js + style.css, vanilla JS + MapLibre; no build step)

Layout: full-viewport map; top bar with two tabs (**Map** / **Feed**), notification banner strip.

Map tab:
- Base: OSM raster tiles (attribution required). Overlays in order: grey zone (hatched/diagonal
  pattern fill, ~35% opacity), RU layer (red fill 25%, darker red 1.5px outline = the frontline),
  event icons.
- Event icons: small circle markers colored by perspective (ukrainian #0057B7, russian #D52B1E,
  western #2E7D32, neutral #757575) with a glyph per `event_type` (advance ▲, claim △, strike ✸,
  shelling ●, geoproof ◎, debunk ⊘). Legend panel explains colors, glyphs and the grey-zone rules.
- Click icon → popup: title (link to original), source name + perspective badge, event type,
  timestamp, confidence; cluster click → zoom to `expansion_bbox`.
- Controls: type/perspective filter checkboxes, time-range select (24h/72h/7d), auto-refresh every
  `poll_seconds` (ETag-aware fetch; only re-render on 200).
Feed tab:
- Infinite-scroll list from `/api/news`; each row: perspective badge (colored pill with the word
  ukrainian/russian/western/neutral), **source site/channel name**, title linking out, relative
  time, event-type chips; a "show on map" button when any event is placed (switches tab, flies to
  point, opens popup). Filters mirror the map's.
Accessibility: all colors also encoded by glyph/text, dark-mode friendly palette via CSS variables.

## 17. Admin backend (blueprint `/admin`, flask-login sessions, bcrypt, CSRF on all forms,
rate-limit login 5/min/IP, every mutation → `audit_log`)

Pages:
1. **Dashboard** — poller health table (status, last success, failures), pending LLM count, last
   frontline build, degraded sources.
2. **Sources** — list/filter; create/edit (name, type, url, perspective, tier, interval, enabled,
   meta); "test fetch" button runs the adapter once inline and shows the first 3 parsed items;
   reset-failures action.
3. **News & events** — search news items; hide/unhide (`visible`), delete, re-run extraction on one
   item; edit an event's location (moves `geom`, sets `coord_source='gazetteer_match'`, audit-logs).
4. **Claims review** — pending/confirmed/reverted claims with their evidence chains (each link:
   source, perspective, role, active, link to original); buttons: confirm, reject, revert,
   reactivate evidence. Manual actions set `resolved_by='admin:<user>'` and the dirty flag.
5. **Notifications** — CRUD with level and time window.
6. **Blackout zones** — MapLibre canvas with polygon draw/edit (vendor mapbox-gl-draw or
   terra-draw); list with active toggle. Effect is defined in §15 (publish-time filtering only —
   ingestion continues, history reappears when deactivated).

## 18. Caching, efficiency, ops

- Redis response cache: key `api:<path>:<sorted-params-hash>` → `(etag, body)`; TTL 120 s for
  /api/events and /api/news, invalidation-only for /api/frontline. Client `If-None-Match` → 304.
- Outbound HTTP: shared `httpx` client, 20 s timeout, `HTTP_USER_AGENT`, conditional GETs, gzip.
- Logging: structlog-style JSON to stdout; per-task timing; never log secrets or full LLM bodies.
- Backups: nightly `pg_dump -Fc` to `./backups` volume via a cron sidecar or host cron; document
  restore in README.
- Metrics-lite: counters in Redis (`stats:llm_calls`, `stats:items_ingested:<day>`), surfaced on
  the admin dashboard. No Prometheus stack (node budget).

## 19. Build phases — implement in order; each ends green and deployable

1. **Foundation**: config, extensions, models, alembic init + first migration, docker-compose with
   postgis/nginx, `seed_sources.py`, `seed_gazetteer.py`, `create_admin.py`, `/healthz`.
   *Done when*: `docker-compose up` boots all services healthy; seeds run idempotently; tests pass.
2. **Baseline map**: deepstate + isw adapters and tasks, frontline builder v1 (baseline + symdiff
   grey, no claims), `/api/frontline`, SPA map tab rendering both layers.
   *Done when*: map shows a current frontline + grey zone from live upstreams; rebuild < 30 s;
   upstream outage serves last snapshot.
3. **Ingestion + feed**: rss/telegram adapters, dispatch_polls, news_items dedupe, `/api/news`,
   feed tab with perspective badges and source names.
   *Done when*: ≥ 10 enabled sources across ≥ 3 perspectives ingest without duplicates; 304/ETag
   flow verified for RSS; degraded-source handling observable in DB.
4. **LLM + geocoding**: llm service, prefilter, extraction task, coordinate regex, gazetteer
   matching, `/api/events`, map icons + popups + filters, geoconfirmed & warspotting adapters.
   *Done when*: a fixture batch extracts correctly (mocked LLM in tests, live via proxy in stage);
   no event without gazetteer match or explicit coords appears on the map; icons clickable.
5. **Rule engine**: claims, evidence links, corroboration/geoproof/debunk logic, deep-strike
   exclusion, builder applies claims, claim badge in popups.
   *Done when*: §20 scenario tests pass end-to-end.
6. **Admin**: auth + all six pages + audit log + blackout filtering in public API.
   *Done when*: full CRUD works; blackout hides events/news in public API but DB keeps ingesting;
   claim override rebuilds frontline.
7. **Hardening**: nginx tuning, backups, rate limits, `.env.example`, README rewrite (deploy,
   restore, admin bootstrap), basic load check (≥ 50 rps on cached endpoints on the target node).

## 20. Testing (pytest; CI-runnable with postgres+redis services; LLM always mocked in tests)

- Unit: adapters parse recorded fixtures (store real captured samples under `tests/fixtures/` for
  deepstate JSON, ISW geojson, geoconfirmed CSV head, warspotting JSON, an RSS feed, a t.me/s HTML
  page); geocode normalization/translit; coordinate regex; prefilter regex.
- Rule-engine scenarios (the spec's acceptance core):
  a. RU-perspective + western-perspective reports on same settlement → claim confirmed (3.1);
     two same-perspective sources → stays pending.
  b. Single geolocation_proof → confirmed (3.2).
  c. Debunk matching that proof → evidence inactive → claim reverted → next build removes the
     geometry change (assert snapshot geometry).
  d. Strike 100 km behind the line → icon exists, no claim, grey zone unchanged (3.3).
  e. Tier-3 sources alone can never confirm.
- API: ETag/304, bbox filtering, blackout filtering, cursor pagination.
- Builder: symdiff grey zone on synthetic polygons; revert idempotence (rebuild twice → identical).

## 21. Risks & fallback rules

- **Upstream renames/breaks** (ISW renames services; DeepStateMap may throttle): adapters fail
  soft, keep last snapshot, mark source degraded, surface on admin dashboard.
- **Telegram markup drift**: adapter isolated; markup-change detector (zero messages parsed from a
  200 page) → degraded, not crash.
- **LLM/proxy outage**: items stay `pending`; system fully functional minus new extractions.
- **Geo-hallucination**: §12 firewall; additionally events with `confidence < 0.4` render at 50%
  opacity and never count as claim evidence.
- **Perspective gaming**: corroboration counts perspectives, not sources; tier-3 excluded (§13).
- **ToS/legal**: only ingest sources catalogued as Free; show attribution + link out in every
  popup/feed row; respect robots.txt for `scrape` type; Liveuamap and other commercial APIs stay out.
