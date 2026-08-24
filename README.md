# Ukraine Frontline Aggregator

A live map of the war in Ukraine: a rendered frontline with an explicit **grey zone**, geolocated
news events from Ukrainian, Russian, Western and neutral sources — every item labelled with its
perspective and its source — and an evidence-based rule engine that only moves the line when
independent perspectives corroborate a change, or a verified geolocation proves it.

Built to run on a single 2 vCPU / 4 GB node. LLM inference is off-node behind a LiteLLM proxy.

```
browsers ── nginx ──► web (Flask/gunicorn, stateless)      Redis ── broker + response cache
                       ├── /          MapLibre SPA         PostGIS ── sources, news, events,
                       ├── /api/*     GeoJSON + news JSON              claims, snapshots,
                       └── /admin/*   session-auth admin               gazetteer, blackouts
                                        ▲
              celery beat + worker ─────┘   pollers → llm_extract → geocode → rules → frontline
                                            └──► LiteLLM proxy (external)
```

## What it does

**Frontline and grey zone.** DeepStateMap (Ukrainian OSINT) and ISW/Critical Threats (Western
assessment) are pulled independently. Where they *agree* is rendered as Russian control; where they
*disagree* — plus DeepState's own "unknown status" areas and a buffer around every unconfirmed
claim — is rendered as grey zone. Two unrelated sources currently agree to within ~3% on total
occupied area, which is what makes the disagreement band meaningful.

**Geolocated events.** RSS feeds and public Telegram channels are polled, deduplicated, filtered by
a war-relevance regex, then run through an LLM that extracts event type, settlement names and any
literal coordinates. Place names are resolved **only** against a local gazetteer of 32,000+
settlements — there is no external geocoder, and an event with neither a gazetteer match nor
explicit coordinates never gets a map position. That is the hallucination firewall.

**Perspective labelling.** Every item carries `ukrainian` / `russian` / `western` / `neutral`, taken
from the source record and never from the LLM, so it is deterministic and auditable.

**Everything in English, originals one click away.** Headlines and snippets are translated by the
same LiteLLM model that does extraction, stored alongside the untouched original. Every translated
item carries a small *original* button. English-language feeds are detected and skipped rather than
round-tripped.

**A private shortlist.** Click ★ on any report to save it; saved spots turn yellow on the map and
can be filtered to on their own, on both the map and the feed. The list lives in `localStorage` —
never sent to the server, so it is private to that browser.

**Two ways to colour the map.** A "Colour by" toggle switches between **perspective** (who is
reporting it) and **beneficiary** (who the event favours — blue for Ukraine, red for Russia, grey
where neither or unassessed). The two often disagree, which is the interesting part: a cluster can
be blue by perspective and red by beneficiary, meaning Ukrainian outlets reporting Russian gains.
Verified geolocations claim no advantage and stay grey rather than being guessed at.

**Time travel.** The map scrubs back by hour and date. Every event carries `occurred_at` (when it
was reported) as distinct from `created_at` (when we ingested it), and every frontline snapshot
carries `valid_at` (the instant it depicts) as distinct from `built_at`. Scrubbing to a past moment
shows the frontline as it was published then, plus the reports from the window ending at that
moment. `scripts/backfill_history.py` rebuilds the dataset from each upstream's own archive.

**Evidence-based frontline changes.** A reported change is confirmed when sources from **≥2 distinct
perspective classes** report it, or when a single **verified geolocation** proves it. An
unconfirmed sighting paints a small grey halo, but only while it keeps being reported: a
geolocation that is not repeated within `GREY_CLAIM_TTL_DAYS` (7) stops contributing to the grey
zone, though the claim itself stays on the books for audit. Because
geolocations can be faked, a later *debunk* retracts the proof and **reverts** the change. Strikes
far behind the line get icons but never move the frontline. Low-confidence extractions and
low-reliability sources can never confirm anything on their own.

## Quick start

```bash
cp .env.example .env          # then fill in the LiteLLM settings and a SECRET_KEY
./deploy.sh                   # builds and starts the whole stack
```

Then, once Postgres reports healthy:

```bash
docker compose exec web alembic upgrade head          # schema
docker compose exec web python scripts/seed_sources.py    # source catalogue (~214, 33 enabled)
docker compose exec web python scripts/seed_gazetteer.py  # 32k settlements from GeoNames
docker compose exec web python scripts/create_admin.py    # uses ADMIN_USERNAME/ADMIN_PASSWORD
```

The site is on <http://localhost>, the admin on <http://localhost/admin>, health on `/healthz`.
All three seed scripts are idempotent — re-running them inserts nothing new.

Nothing appears on the map until the first upstream pull and build, which `celery beat` does within
30 minutes. To not wait:

```bash
docker compose exec worker python -c "
from celery_worker.tasks.frontline import pull_deepstate, pull_isw, rebuild_frontline
print(pull_deepstate()); print(pull_isw()); print(rebuild_frontline(force=True))"
```

### Backfilling history

The live pollers only ever see "now". To populate the time scrubber, walk the archives:

```bash
# Daily frontline geometry from DeepStateMap's 1,736 published snapshots (back to 2022-04-03)
docker compose exec web python scripts/backfill_history.py --frontline --since 2025-01-01

# GeoConfirmed's verified geolocations — already dated, no LLM needed
docker compose exec web python scripts/backfill_history.py --geoconfirmed --since 2025-01-01

# WarSpotting per-date losses, and Telegram channels paged backwards
docker compose exec web python scripts/backfill_history.py --warspotting --days 365
docker compose exec web python scripts/backfill_history.py --telegram --days 90 --max-pages 40
```

All of it is idempotent — content hashes, `(provider, upstream_version)` and `(layer, valid_at)`
are unique, so a re-run resumes instead of duplicating. Budget roughly **0.5 MB per frontline day**
(`--per-day 1` keeps one snapshot per day; `--per-day 0` keeps all of them, up to 4/day).

Telegram backfill only *ingests* text; those items still queue for LLM extraction, which runs at
roughly 11 batches/min against the reference proxy — check the pending count on the admin
dashboard before backfilling a large window.

### Local development

`docker-compose.override.yml` publishes Postgres on `55432` and Redis on `56379` so tests, seeds and
`psql` can reach them from the host. It is development-only — deploy with
`docker compose -f docker-compose.yml up -d`, or delete the file.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
docker compose up -d postgis redis
.venv/bin/python -m pytest -q            # 189 tests; skips cleanly if the datastores are down
```

## Configuration

`.env` (see `.env.example`). The stack boots without the LLM settings — it simply stops extracting.

| Variable | Default | Meaning |
|---|---|---|
| `LITELLM_MODEL` / `LITELLM_API_KEY` / `LITELLM_API_BASE` | — | LiteLLM proxy, OpenAI-compatible |
| `DATABASE_URL` | `postgresql+psycopg://ukraine:ukraine@postgis:5432/ukraine` | SQLAlchemy DSN |
| `REDIS_URL` | `redis://redis:6379/0` | broker, results, response cache |
| `SECRET_KEY` | random at boot (warns) | Flask sessions — **set this**, or sessions die on restart |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | — | read by `scripts/create_admin.py`; bcrypt-hashed into the DB |
| `PUBLIC_POLL_SECONDS` | `90` | refresh hint served at `/api/config` |
| `HTTP_USER_AGENT` | `UkraineAggregator/1.0 (+…)` | all outbound polling — put a real contact URL here |

Algorithm constants live in `app/config.py` and are all env-overridable:
`DEEP_STRIKE_KM` (30), `CLAIM_JOIN_KM` (5), `GREY_BUFFER_KM` (1.5), `GREY_CLAIM_TTL_DAYS` (7),
`GREY_MIN_PART_KM2` (0.5),
`GAZETTEER_MIN_SIMILARITY` (0.55), `LLM_BATCH_SIZE` (8), `LLM_MAX_BODY_CHARS` (4000),
`LLM_REASONING` (none), `LLM_CONCURRENCY` (4 — see `docker-compose.loadtest.yml`),
`SNAPSHOT_SIMPLIFY_TOLERANCES`.

**If your proxy model is not a reasoning model** with a small context window, set
`LLM_REASONING=auto` and raise `LLM_CONTEXT_TOKENS`. Batches are sized to the real context window
automatically, so `LLM_BATCH_SIZE` is an upper bound rather than a fixed count.

## Public API

| Endpoint | Notes |
|---|---|
| `GET /api/config` | poll interval, map defaults, event types, colours, glyphs |
| `GET /api/frontline?detail=low\|mid\|high&at=ISO` | `{valid_at, built_at, layers:{ru, grey}, meta}`. `at` returns the newest snapshot valid at or before that instant |
| `GET /api/events?bbox=&from=&to=&types=&perspectives=&limit=&zoom=&cluster=off` | GeoJSON; clusters above 500 matches unless `cluster=off`. Windowed on `occurred_at`, so backfilled history lands on the right date |
| `GET /api/news?cursor=&perspective=&source_id=&q=&limit=` | newest-first, cursor-paginated |
| `GET /api/timeline` | scrubber bounds, the instants snapshots exist for, and events per day |
| `GET /api/notifications` | active banners within their time window |
| `GET /healthz` | db, redis, last build, pending extractions, degraded sources |

Every response is ETagged; send `If-None-Match` and expect `304`. Blackout zones are applied here,
at publish time only — ingestion continues behind them, and deactivating a zone makes its history
reappear.

## Admin

`/admin`, session auth, CSRF on every form, login rate-limited to 5/min/IP, every mutation written
to `audit_log`.

1. **Dashboard** — poller health, pending extractions, last build, counters, recent audit trail.
2. **Sources** — filter, create/edit, enable/disable, reset failure counters, and *test fetch*,
   which runs a source's adapter inline and shows the first three parsed items.
3. **News & events** — search, hide/unhide, delete, re-run extraction, correct an event's location.
4. **Claims review** — pending/confirmed/reverted claims with their full evidence chains; confirm,
   reject, revert, or activate/deactivate individual pieces of evidence.
5. **Notifications** — CRUD with level and time window.
6. **Blackout zones** — draw polygons on a map; toggle and delete.

## Operations

**Backups.** The `backup` service runs `pg_dump -Fc` daily into `./backups`, keeping 14 days.
Restore:

```bash
docker compose stop web worker beat
docker compose exec -T postgis pg_restore -U ukraine -d ukraine --clean --if-exists < backups/ukraine-YYYYMMDD.dump
docker compose start web worker beat
```

**Source health.** Adapters fail soft. Five consecutive failures mark a source `degraded`, twenty
mark it `dead` and stop polling it until an admin resets it. HTTP 403 (several catalogued sites
block datacenter IPs) marks `degraded` but never `dead`. A Telegram channel whose page parses to
zero messages five times running is flagged as markup drift rather than crashing the poller.

**Upstream outages.** If DeepStateMap or ISW is unavailable the last stored geometry keeps serving
and the build records `degraded: single_upstream:<provider>`; if the LLM proxy is down, items stay
`pending` and everything else keeps working.

**Scaling.** The web tier is stateless — replicate it behind nginx. Work is split across three
queues: `control` (the dispatchers that decide what runs), `default` (polling, geocoding, rules,
the frontline builder) and `llm` (extraction and translation, on its own `llmworker` at
`LLM_CONCURRENCY`, default 4 — the measured saturation point of the reference proxy). The
dispatchers are queue-depth aware, so neither polling nor LLM work can pile up unboundedly or
starve the other.

## Documentation

- `plans/PLAN.md` — the full build specification.
- `plans/DEVIATIONS.md` — **where reality diverged from the spec and what the code does instead.**
  Worth reading: several entries are correctness-critical, including DeepStateMap polygon
  classification (a naive read over-reports occupied territory by 2.6×) and gazetteer matching
  (whole-string similarity made major frontline cities unmatchable).
- `sources/VERIFIED_ENDPOINTS.md` — probe logs for the external APIs.

## Attribution and limits

Base tiles © OpenStreetMap contributors. Frontline geometry from
[DeepStateMap](https://deepstatemap.live) and [ISW / Critical Threats](https://understandingwar.org).
Geolocations from [GeoConfirmed](https://geoconfirmed.org) and
[WarSpotting](https://ukr.warspotting.net). Gazetteer from [GeoNames](https://geonames.org)
(CC BY 4.0). Only sources catalogued as freely accessible are ingested; every popup and feed row
links back to the original. Commercial APIs are deliberately out of scope.

This aggregates contested claims from parties to a war. The grey zone, the perspective labels and
the evidence chains exist so a reader can see *who* said something and *what corroborates it* —
not to present any of it as settled fact.

## License

MIT — see `LICENSE`.
