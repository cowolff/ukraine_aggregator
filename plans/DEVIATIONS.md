# Deviations from `plans/PLAN.md`

The spec says: where it and reality diverge, prefer reality, log the divergence here, and keep the
§15 interface contracts stable. All §15 contracts are unchanged. Each entry below states what the
spec said, what was actually observed, and what the code does instead.

---

## 1. DeepStateMap polygons need status classification, not just a geometry-type filter

**Spec (§8.1):** "Polygons are **occupied/Russian-controlled areas** … Features include areas
outside Ukraine context; keep only polygons."

**Reality:** keeping every polygon over-reports occupied territory by ~2.6×. The 122 polygons in
one snapshot decompose, via a token embedded in each polygon's `name`, into:

| Class | Polygons | Area |
|---|---|---|
| `status.occupied` + `territories.crimea` + `territories.ordlo` — actually occupied | 27 | ~117,000 km² |
| `status.unknown` — DeepState's **own declared grey zone** | 31 | ~1,700 km² |
| `status.dismissed` / `dismissed_at` — **liberated** territory | 50 | ~42,000 km² |
| `territories.*` elsewhere — Karelia, Kuril, Prussia, Abkhazia, Transnistria, Estonia, Latvia, … | 13 | ~165,000 km² |

The naive union yields 299,634 km²; the classified occupied set yields 117,007 km², against ISW's
independently assessed 113,167 km² — a 3% spread between two unrelated sources, which is what
makes §14's intersection/symmetric-difference meaningful at all. Unioning liberated territory into
"occupied" would have inflated Russian control by an area the size of Denmark.

**Implementation:** `celery_worker/adapters/deepstate.py` classifies by token.
`status.unknown` is stored separately as `deepstate_grey` and fed into the grey zone directly —
control stated as unclear at the source is better evidence than any derived geometry.
A plausibility band (50,000–250,000 km², `store_upstream(plausible_km2=…)`) makes an upstream token
rename fail soft instead of silently emptying the map.

## 2. Grey zone: sliver filtering

**Spec (§14.2):** grey zone = `ST_SymDifference(DS, ISW)` unioned with pending-claim buffers.

**Reality:** two independently hand-drawn datasets never trace the same line identically, so the
raw symmetric difference contained 1,239 polygons of which 1,085 were under 0.5 km² — 1% of the
grey area but 19% of its vertices, rendering as noise along the whole front.

**Implementation:** parts of the *derived* disagreement band below `GREY_MIN_PART_KM2` (default
0.5) are dropped; DeepState's declared grey polygons and claim buffers are never size-filtered.
Result: 155 polygons retaining 99% of the area. Set `GREY_MIN_PART_KM2=0` for the literal spec
behaviour.

## 3. Gazetteer source: GeoNames instead of KATOTTG + Overpass

**Spec (§8.5):** download the KATOTTG codifier, join coordinates from OSM via Overpass chunked by
oblast, target ≥ 25k settlements.

**Reality:** the Overpass path needs 27 area queries and could not complete. The main endpoint
refused connections from the build host; two independent mirrors returned HTTP 500 on individual
oblasts. Seventeen of 27 oblasts failed, leaving ~10,000 settlements — well short of the target.

**Implementation:** GeoNames `UA.zip` is the default source: one 2.2 MB download, no rate limit,
33,700 populated places with coordinates, population, oblast codes and Ukrainian/Russian/Latin
name variants already present. Loads 32,376 settlements, exceeding the §8.5 target.
`--source overpass` is retained (it carries settlement polygons GeoNames lacks, useful for
`gazetteer.boundary`), and `--katottg FILE.csv` still attaches the authoritative admin codes when
the codifier export is supplied.

## 4. Gazetteer upsert key includes proximity

**Spec (§8.5):** "idempotent (upsert by katottg, else by `(name_uk, oblast)`)".

**Reality:** Ukrainian oblasts routinely contain several distinct villages of the same name. On the
first Donetsk load, `(name_uk, oblast)` collapsed 1,298 places into 965 — silently discarding a
quarter of the oblast.

**Implementation:** the no-KATOTTG key is `(name_uk, oblast, within 3 km)`. Re-runs still match the
same settlement (verified: second run inserts 0 rows), while genuinely different same-named
villages both survive.

## 5. Gazetteer matching uses `word_similarity`, not `similarity`

**Spec (§12.2):** `SELECT id, similarity(name_search, :q) s … WHERE name_search % :q`.

**Reality:** `name_search` holds every name variant concatenated, and whole-string `similarity`
divides by total length — so it penalises exactly the well-documented settlements it should
favour. Measured: a full match on "kupiansk" scored 0.281, below the 0.55 acceptance threshold, so
Kupiansk was **unmatchable**; and Kostiantynivka in Donetsk (pop. 78,179 — a frontline city)
scored 0.652 against 0.833 for an 8-person hamlet of the same name, so reports about it were
placed three oblasts away.

**Implementation:** `word_similarity(:q, name_search)` (operator `<%`, served by the same GIN
trigram index) scores the best-matching variant inside the blob. Because it saturates at 1.0,
scores within 0.05 are treated as tied and broken by population — a report naming "Киев" means the
capital. An oblast hint is applied **in SQL** rather than to an already-truncated candidate list,
so the right settlement cannot be cut off before the filter runs. `GAZETTEER_MIN_SIMILARITY`
keeps its 0.55 default and now means "word similarity".

## 6. Alternate-name variants are selected by relevance, not arrival order

Well-known cities carry hundreds of GeoNames alternates, so the per-settlement variant list must be
capped. Taking the first N dropped the forms that matter — Kyiv's Russian "Киев" fell off the end,
and reports spelling it that way resolved to a like-named village. Variants are now ranked by how
closely their transliteration matches the ASCII name (`VARIANT_MIN_RATIO`, `VARIANT_LIMIT`), which
keeps real transliterations and sheds distant exonyms.

## 7. LLM client: reasoning disabled, context-aware batching

**Spec (§4, §11):** `LLM_BATCH_SIZE` 8, `LLM_MAX_BODY_CHARS` 4000, temperature 0,
`response_format: json_object`.

**Reality:** the configured proxy model (`hosted_vllm/qwen3.5-9b`) is a **reasoning** model with an
**8,192-token total** context shared between input and output. Three separate failures followed:

1. A fixed 8 × 4000-char batch exceeded the context window outright (HTTP 400
   `ContextWindowExceededError`) — Cyrillic tokenises at roughly 2 characters per token, so the
   spec's batch is ~4× too large for this model.
2. With a workable batch, the model spent its entire output budget emitting `reasoning_content`
   and returned `content: ""` with `finish_reason: length` — no JSON at all.
3. The generic 20 s HTTP timeout (×3 in `llm.py`) was far too short: a *trivial* prompt took 20 s
   with reasoning on.

**Implementation:**
- `LLM_REASONING=none` (default) sends `reasoning_effort: "none"` **and**
  `chat_template_kwargs: {enable_thinking: false}`, covering both the OpenAI-compatible and
  vLLM/Qwen spellings; a proxy that rejects either falls back automatically, as it already did for
  `response_format`. Measured effect on a trivial prompt: 6.5 s and an empty answer → 0.4 s and
  clean JSON. Extraction is mechanical transcription and gains nothing from a chain of thought.
- `LLM_BATCH_SIZE` becomes an *upper bound*: `llm.fit_batch()` sizes each batch to the real context
  window (discovered from the proxy's `/models`, falling back to `LLM_CONTEXT_TOKENS`), reserving
  `LLM_MAX_TOKENS` for output. A single oversized item is truncated rather than wedging the queue.
- `LLM_TIMEOUT_S` (default 300) replaces the generic HTTP timeout for extraction calls.
- If `content` is empty but `reasoning_content` is not, the JSON is recovered from the reasoning
  channel rather than discarding the call.

These are all configurable: a non-reasoning model with a large context needs no changes beyond
`LLM_REASONING=auto`.

## 8. Blackout-zone drawing is hand-rolled

**Spec (§17.6):** "vendor mapbox-gl-draw or terra-draw".

**Implementation:** a ~60-line click-to-place-vertices polygon tool directly on the MapLibre canvas
(`app/admin/templates/blackouts.html`). mapbox-gl-draw targets mapbox-gl rather than maplibre-gl and
would have pulled in a second map library for one admin page. The tool writes GeoJSON into an
editable textarea, so a polygon can also be pasted in from elsewhere.

## 9. nginx resolves the web upstream at request time

Not a spec deviation but a deployment fix worth recording: with a static `upstream web { server
web:5001; }`, nginx resolves the container IP once at startup and then 502s permanently after `web`
is recreated (image rebuild, restart, rescheduling) — observed during this build. The upstream is
now addressed through a variable with Docker's embedded resolver (`127.0.0.11`), so DNS is
re-resolved per request. Verified by recreating `web` and re-requesting.

## 10. `/api/events` default time window is snapped to a cache bucket

**Spec (§15):** "Default window: last 72 h", responses Redis-cached with a 120 s TTL.

**Reality:** computing "now − 72 h" per request put a microsecond-precision timestamp in the cache
key, so every poll minted a fresh key and both the Redis response cache and client ETags never hit
— the busiest endpoint would have gone to Postgres on every request from every client.

**Implementation:** the default window start is snapped down to an `API_CACHE_TTL_S` bucket, so
identical requests within a TTL share a cache entry. An explicit `from` parameter is unaffected.
`/api/events` also accepts a space where a `+` offset should be, since an unencoded `+` in a query
string decodes to a space.

## 11. Additional schema columns

Three columns not in §7, all additive:

- `news_items.llm_attempts` — implements §11's "failed items retry max twice more".
- `news_items.visible` — §17.3 requires hide/unhide for news items, not only events.
- `extracted_events.linked` — marks an event as having passed through rule intake, so §13 intake is
  exactly-once and cheap to query.

Plus one table, `upstream_geometries`: the raw DeepState/ISW layers as builder input. §14.3 requires
recomputing from scratch on every build so reverts are automatic; storing upstream geometry
separately means a rebuild never re-fetches, and it is what makes the nightly forced rebuild and
revert-idempotence test cheap.

## 12. Ingested source count

**Spec (§9):** "do NOT import all 538 rows … import a curated working set", starter set ~25.

**Implementation:** 214 pollable sources imported (RSS + Telegram, free-access rows only), of which
33 are enabled across all four perspectives (7 Ukrainian RSS, 8 Russian Telegram, 7 Western RSS,
4 neutral Telegram, the 4 §8 APIs, and others). The rest are stored disabled for the admin to
enable. Rows whose reliability notes mark them preview-disabled or dormant are skipped entirely.

## 13. Frontend resilience additions (§16)

Three additions beyond the spec's frontend description, all found by rendering the page in a real
browser:

- **Overlays install on style-ready, not on `load`.** MapLibre's `load` event waits for the base
  raster tiles as well as the style, so an unreachable tile server meant the frontline and event
  layers — the ones served by our own API — never initialised at all. They are now installed on
  `styledata`/`load` (whichever comes first) with a short watchdog poll, so the map still shows
  the frontline when OSM is blocked or down.
- **Map errors are surfaced.** `map.on('error')` writes the reason into the status bar. Without it,
  a style or tile failure left a black rectangle and no explanation; that silence is what hid the
  point above.
- **WebGL fallback.** If the browser cannot create a WebGL context the map area explains why and
  offers the Feed tab, instead of rendering an unexplained black box.

Also: the two left-hand panels (filters, legend) are laid out in a single flex column. As
independently top- and bottom-anchored elements they overlapped each other at ordinary laptop
viewport heights.

## 14. Telegram post titles

§8.6 specifies parsing message text from `.tgme_widget_message_text`. Taking the first line of that
text as the headline produced titles that were a single decorative emoji — "⚡️", "📝", "🟠", "❗️" —
for a large share of channels, which then appeared as the headline in the feed and in map popups.
`telegram_web.headline()` takes the first line that actually contains a letter or digit, joining
following lines while the result is too short to stand alone.

---

# Findings from the large-scale LLM load test

A 4,802-item corpus from 182 live sources was run through the extraction pipeline against the
LiteLLM proxy at worker concurrencies of 1, 4, 6 and 8. Four defects surfaced that no unit test
and no small serial run had reached.

## 15. Concurrent extraction batches double-processed every item

`llm_extract_batch` selected its work with `SELECT ... LIMIT`, which takes **no locks**. Every
parallel worker therefore selected the same pending rows, sent them all to the proxy, and wrote
duplicate events. Measured on a single 6-way run: **314 news items with duplicated event groups**,
and events accumulating roughly 10× faster than items completed. The system could not scale past
one extraction worker — directly at odds with §1's scalability requirement.

**Implementation:** a claim step. `_claim()` locks candidate ids with `FOR UPDATE SKIP LOCKED`,
marks them `llm_status='processing'` and commits *before* the proxy call, so no lock is held across
the network round trip and parallel workers glide past each other's rows. Migration
`0002_llm_processing` extends the `llm_status` check constraint. Items trimmed from a batch by
`fit_batch` release their claim. After the fix, ~4,800 items processed at concurrencies up to 8
produced **zero** new duplicates.

Two details worth keeping:
- The lock is taken on `select(NewsItem.id)`, not on the entity. `NewsItem.source` is an eager
  join and Postgres rejects `FOR UPDATE` on the nullable side of an outer join.
- `maintenance` releases claims abandoned by a dead worker. Its first version keyed off
  `fetched_at` — the *ingestion* time — so an item ingested hours ago and claimed one second ago
  already looked stale and would be yanked out from under a running batch, re-creating the very
  duplication claiming prevents. Migration `0003_llm_claimed_at` adds a real claim timestamp
  (`LLM_CLAIM_STALE_MINUTES`, default 30).

## 16. The model repeats locations within a single response

One daily-summary item returned the same empty location **18 times**; several returned the same
settlement two to four times. Each became its own event: redundant on the map, and redundant as
evidence on a claim. `_persist` now collapses identical `(place, lat, lon)` rows within one
response. (Corroboration counts perspectives, not links, so this never manufactured a false
confirmation — but it inflated event counts and map clutter.)

## 17. The war-relevance prefilter discarded ~1 in 5 relevant items

§11's prefilter exists to save tokens, but it was silently dropping real reporting. A 120-item
random sample of prefilter-rejected news was forced through the LLM: **17.5% were judged relevant**
(21 of 120). The regex was missing high-frequency vocabulary, including:

- `атак…` — "attacked", among the most common verbs in this corpus, absent entirely
- `беспилотник` / `безпілотник` — the standard Russian and Ukrainian word for drone (only `дрон`
  and `бпла` were covered)
- English `destroyed`, `damaged`, `losses`, `loses`, `casualties`, `attacked`, `killed`, `wounded`
  — the entire register that equipment-loss feeds like WarSpotting are written in
- loss/casualty vocabulary in both Cyrillic languages (`втрат`, `потер`, `знищ`, `уничтож`,
  `збит`, `сбит`, `поранен`)

`RELEVANCE_PATTERNS` was expanded accordingly. Re-measured over the whole corpus: **491 previously
skipped items (21.3%) now pass**, closely matching the independently measured 17.5% miss rate, and
all 491 extracted successfully. The overall pass rate rises from ~52% to 61.9% — about 19% more
proxy calls in exchange for not losing a fifth of relevant reporting. Obviously-irrelevant control
items (recipes, earnings, weather) are still rejected.

## 18. Measured throughput, and the proxy's saturation point

| Concurrency | Calls/min | Mean latency | Tokens/min | 5xx |
|---|---|---|---|---|
| 1 | 3.7 | 15.5 s | 10,400 | 0 |
| **4** | **11.6** | **19.2 s** | **36,800** | **0** |
| 6 | 9.2 | 35.7 s | 37,600 | 2 |
| 8 | 9.6 | 40.7 s | 33,500 | 5 |

The proxy saturates at **concurrency 4**. Beyond it, latency roughly doubles, 5xx errors appear
and throughput does not improve — a single backend serialising work. `docker-compose.loadtest.yml`
adds a dedicated `llm`-queue worker whose concurrency is set by `LLM_CONCURRENCY`; 4 is the value
to use with this proxy.

The full 2,528-item backlog cleared in ~21 minutes at concurrency 4: 284 calls, 949,521 tokens
(829k in / 120k out), **zero** 429/4xx/5xx errors, zero items stuck in `processing`, 18.3 s mean
latency. Retry and claim-release paths were exercised by the concurrency-6 and -8 runs (7 total
5xx) with no item lost or duplicated.

## 19. Known gap: strikes inside Russia cannot be placed

The unplaced residue is dominated by Russian locations — Moscow, Kursk oblast, Stavropol Krai,
Samara, Tula, Dagestan, Makhachkala — plus oblast-level names and the occasional non-place (a
company name in a market story). This is correct behaviour for the §12 firewall: the gazetteer is
Ukrainian (§8.5), so these have no coordinates and are refused rather than guessed. But deep
strikes on Russian territory are a substantial category of this war's reporting, and they
currently appear in the feed with no map icon. Loading GeoNames `RU.zip` (or just the border
oblasts) into the same gazetteer would close it; that is a scope decision, not a bug.

---

# Time travel (feature added after the spec)

The spec describes a live map. Scrubbing back by hour and date needed a time axis the schema did
not have, plus a backfill path, and it surfaced two latent bugs.

## 20. Ingest time is not event time

Everything was filtered on when a row was *written*: `extracted_events.created_at` and
`frontline_snapshots.built_at`. That is fine while the only data is live, and useless the moment
history exists — a 2024 snapshot backfilled today was *built* today. Migration `0004_time_travel`
adds:

- `extracted_events.occurred_at` — when the reported event happened (the item's publication time)
- `frontline_snapshots.valid_at` — the instant the geometry depicts
- `upstream_geometries.valid_at` — the same for raw upstream layers

`created_at`/`built_at` stay as the ingest audit trail. `/api/events` now windows on `occurred_at`
(a behaviour change: previously a backfilled 2024 report would have shown up as "today's news").
The composite index `(geom, occurred_at)` needs the `btree_gist` extension, which the migration
enables — it makes "in this bbox during this window", the hot query once the map can scrub, a
single index scan.

Historical builds are honest about their inputs: no ISW archive is ingested, so `build(at=…)` for a
past instant finds only DeepStateMap and records `degraded: single_upstream:deepstate`. The grey
zone for those dates comes from DeepState's own "unknown status" polygons rather than from a
cross-source disagreement. A historical build also excludes claims created or resolved after the
instant being rendered, so last year's map is not dusted with this year's pending claims.

## 21. The snapshot pruner would have deleted the entire history

`maintenance` thinned snapshots with `PARTITION BY layer, date_trunc('day', built_at)`, keeping one
row per bucket beyond the retention window. Every backfilled snapshot shares a single `built_at`
(the run that created them) while spanning hundreds of distinct `valid_at` days — so they all
landed in one bucket. They survived only because that build date was recent; once it aged past
`SNAPSHOT_RETENTION_DAYS`, the pruner would have deleted all but one and silently destroyed the
time-travel feature. Partitioning is now on `valid_at`, which both preserves one snapshot per
historical day indefinitely and still collapses the live builder's intra-day duplicates. Three
tests pin the behaviour.

## 22. Frontend: stale responses could overwrite newer ones

Scrubbing while the map moved raced two `/api/events` requests, and the slower live-window response
landed *after* the historical one and overwrote it — the map showed clusters from "now" while the
scrubber read March. Requests now carry a monotonic token per kind and a response is applied only
if no newer request has been issued since. This was reachable in ordinary use too (panning quickly,
or changing filters mid-flight); the scrubber just made it reproducible.

## 23. Static assets were cached for a week with no cache-busting

`expires 7d` on `/static/` meant a deployed JS or CSS fix never reached anyone still holding the old
bundle — found when a fix to the time readout kept not appearing in the browser. Vendored libraries
(version-pinned, never edited in place) keep a 30-day immutable cache; the app's own `app.js` and
`style.css` are served `no-cache` and revalidate against their ETag, so an unchanged file is a 304
rather than a re-download.

## 24. DeepStateMap snapshot ids are publication timestamps

The ids are unix timestamps of the moment each snapshot was published (id `1787509777` ==
`2026-08-23T18:29:37Z`, cross-checked against the history index). The live pull derives `valid_at`
from the id rather than using "now", so a live pull and a backfill of the same snapshot agree on
the instant and re-running a backfill cannot create near-duplicate history.

## 25. What history each source actually offers

| Source | Depth | Cost |
|---|---|---|
| DeepStateMap | 1,736 snapshots, 2022-04-03 → today, up to 4/day | ~560 KB per fetch; ~0.5 MB stored per day kept |
| GeoConfirmed | ~59k verified geolocations back to 2014, already dated | one 31 MB CSV; **no LLM needed** |
| WarSpotting | per-date endpoints going back years; ~half geolocated | one request per day of history |
| Telegram | `?before=<id>` pages ~20 messages at a time, indefinitely | one request per 20 messages, **plus LLM extraction** |
| RSS | current window only — no archive | — |

Only the first three are cheap. Telegram backfill produces free text that must go through
extraction, so a wide window there is an LLM-bound job, not an HTTP-bound one.

---

# Cluster colour: which side's reporting dominates

## 26. Uniform blue clusters contradicted the legend

Zoomed-out clusters were painted a single blue (`rgba(76,141,255,.75)`) — the same hue the legend
assigns to **Ukrainian sources**. A cluster of entirely Russian-perspective reporting therefore
rendered blue, so the most prominent marks on the zoomed-out map actively misinformed.

Clusters now carry a **diverging scale**: Ukrainian blue `#0057B7` ↔ neutral grey `#666c75` ↔
Russian red `#D52B1E`, interpolated in Lab so the ramp is perceptually even. The poles are the same
hues the individual markers use, so a cluster reads as "more of these". Size still encodes count.

Design decisions worth recording:

- **Two hues plus a *neutral grey* midpoint**, never a third hue at the middle — a balanced cluster
  must not look like a third category.
- **The midpoint was chosen against the real map surface, not a chart background.** The usual
  near-white diverging midpoint scores **1.0:1** against OSM's land fill and disappears entirely.
  Contrast was measured against the four tile colours clusters actually sit on (land `#f2efe9`,
  forest `#c8d7ab`, water `#aad3df`, urban `#e6e3dd`). `#666c75` holds ≥ 3:1 on all four while
  giving the widest colourblind separation from both poles (12.2 ΔE protanopia, 16.2 normal
  vision, all-pairs). Lighter greys separate better but drop below 3:1 on water and forest.
- The palette validator's **chroma-floor check fails on the midpoint by design** — a diverging
  midpoint is *supposed* to read as grey. The categorical checks that matter here (CVD separation,
  normal-vision floor, contrast) all pass.
- **Western and neutral sources are excluded from the axis.** They are not "half Ukrainian"; a
  wire-service cluster reports as having no lean rather than as balanced. A cluster with no
  Ukrainian or Russian sources at all sits at the same neutral midpoint, because it has no lean to
  show either.
- **Small samples are damped.** A raw ratio paints a cluster holding one Russian report fully red.
  A pseudo-count of 2 (`(ru + 1) / (ru + ua + 2)`) pulls small samples toward neutral and lets
  large ones reach the poles: 1 report → 0.67, 20 → 0.95. Without this the zoomed-out map would
  look far more polarised than the evidence supports.
- **Colour is not the only carrier.** Hovering a cluster gives the exact breakdown ("113 reports ·
  mostly Ukrainian-perspective reporting · 70 ukrainian, 33 russian, 7 western, 3 neutral"), and
  the legend shows the ramp with its end labels. Dark mode needed no separate steps: the map
  surface is the same raster tiles in both themes.

## 27. Popups rendered behind the map panels

Found while testing the cluster tooltip, but it affected every event popup: the filter/legend rail
(`z-index: 5`) and the time bar (`6`) sat above MapLibre's popups, so a popup opened near the left
edge or the bottom centre was partly unreadable. Popups are now `z-index: 8`.

## 28. Second colouring axis: perspective vs beneficiary

A "Colour by" toggle switches what the map's colours mean:

- **Perspective** (default) — *who is reporting it*, from the source record.
- **Beneficiary** — *who the event favours*, from `extracted_events.claimed_by`, which the §11
  extraction prompt already defines as "which side the reported gain/position favors".

The two routinely disagree, and that disagreement is the point. A measured example from live data:
the largest cluster in the Donbas held 66 Ukrainian-source reports against 32 Russian — solidly
blue on the perspective axis — while 96 of its 106 events *favoured Russia*, making it strongly
red on the beneficiary axis. Ukrainian outlets reporting Russian advances.

Both axes ship in every `/api/events` response (`counts`/`lean` and
`beneficiary_counts`/`lean_beneficiary`, plus `beneficiary` on point features), so switching mode
repaints instantly with no refetch. The diverging ramp, the small-sample damping and the neutral
midpoint are shared between them; `perspective_lean` was renamed `side_lean` accordingly.

**GeoConfirmed's `Faction` is deliberately *not* mapped to beneficiary.** It records whose asset a
placemark concerns, not who gains: sampling the archive turns up Faction=Russia entries like
"Yelabuga drone factory", "Pantsir air defense system in Kazan" and "air defense ramp near
Moscow" — Russian assets *documented by OSINT*, which is closer to a Ukrainian intelligence gain
than a Russian one. Treating that column as a beneficiary would have painted ~5,700 verification
records as "good for Russia". Those events are reported as **unassessed** and take the neutral
grey instead.

Coverage follows from that choice, and is honest about itself:

| Window | Events | With a beneficiary assessment |
|---|---|---|
| last 72 h | 2,100 | **95%** |
| last 7 days | 2,463 | **92%** |
| full archive | 14,060 | 20% |

The mode is fully informative in the windows people actually use; over long historical ranges the
11k backfilled GeoConfirmed geolocations dominate and most of the map goes grey. The legend says
why ("verified geolocations carry no judgement of advantage") rather than leaving it a mystery.

## 29. A `const` used before its declaration broke page boot

`BENEFICIARY_COLORS` was declared next to the `State` object but referenced `LEAN_UA`/`LEAN_RU`,
which are declared further down — a temporal-dead-zone `ReferenceError` at module evaluation, so
`boot()` never ran and the map never initialised. `node --check` does not catch this (the syntax is
valid); it needs the module to actually be evaluated. The constants are now declared above their
first use.

## 30. Grey halos: smaller, and they expire

Two changes to how an unconfirmed claim contributes to the grey zone (PLAN §14.2).

**Radius.** `GREY_BUFFER_KM` dropped from 3 km to **1.5 km**. A 3 km radius is ~28 km² per
sighting — far larger than the settlements these claims describe, and with ~100 open claims it
buried the actual disagreement band under circles. 1.5 km (~7 km²) is closer to a settlement's own
footprint.

**Expiry.** A single sighting is a one-off report, not evidence of an ongoing contested area. A
pending claim now paints grey only while its newest *active* evidence is within
`GREY_CLAIM_TTL_DAYS` (default **7**); after that the halo disappears. Three details worth
recording:

- The recency test uses the evidence's `occurred_at` — when the sighting was reported — not when it
  was ingested. A backfilled report therefore ages from its own date, and a historical rebuild asks
  "was this still fresh *then*", so scrubbing back shows the halo as it actually stood.
- **Only the halo expires.** The claim stays `pending`, keeps its evidence chain, and can still be
  confirmed later if the sighting is repeated or corroborated. Formal rejection remains a separate,
  longer clock (`CLAIM_STALE_DAYS`, 14 days) so the audit trail is unaffected.
- Evidence deactivated by a debunk stops counting as a repeat, so a retracted sighting cannot keep
  an area grey.

Measured on live data at the time of the change: of 96 pending claims, 64 were still being
reported and 32 had gone quiet. Combined with the smaller radius the grey zone fell from
**7,567 km² to 5,518 km²** (−27%). The ~2,050 km² difference is entirely halo area — the
DeepStateMap/ISW disagreement band and DeepState's declared "unknown status" polygons are
untouched, which is the intent: the tightening removes speculation, not evidence.

---

# Automated translation and a private shortlist

## 31. English translations stored beside the original

Most of the corpus is Ukrainian or Russian, so the public site now displays an English rendering.
Migration `0005_translations` adds `title_en`, `body_en`, `translation_status` and
`translation_claimed_at` to `news_items`; the original text is **never overwritten**, and every
translated item ships both versions so the UI's *original* button needs no extra request.

`tasks.translate_batch` runs on the same `llm` queue as extraction and deliberately reuses its
machinery rather than duplicating it: rows are claimed with `FOR UPDATE SKIP LOCKED` before any
proxy call (see §15), batches are sized to the model's real context window, and reasoning is off.
`maintenance` releases abandoned translation claims on the same clock as extraction claims.
`fit_batch` gained a `system_prompt` argument so it budgets against the prompt actually being sent.

**What gets translated.** Cyrillic text always. Otherwise the *source's catalogued language*
decides, because Latin script alone does not mean English — the catalogue carries Polish, Czech and
Romanian outlets. With no language recorded and no Cyrillic, an item is assumed English and skipped
rather than spending tokens on a no-op. `scripts/seed_sources.py` now carries the CSV's `language`
column into `sources.meta` (210 of 214 sources have one); a re-run backfills existing rows.
Multi-language feeds ("Ukrainian, English") are handled by the Cyrillic test, which catches their
non-English posts regardless of the label.

Bodies are truncated to 1,200 characters — harder than for extraction — because the feed shows a
headline and a snippet, and a full Telegram essay would crowd the rest of the batch for no display
benefit. Measured throughput: 8 items per call, 12–27 s per batch.

## 32. A shortlist that never leaves the browser

Readers can flag reports with ★. Saved markers render **yellow on the map whatever the colour
mode** — the reader's own flag outranks the editorial encoding — and both the map and the feed can
filter to the shortlist alone.

- Stored in `localStorage` under `ukraine-aggregator:saved:v1`, **never sent to the server**. Writes
  are wrapped in try/catch so private-browsing or a full quota degrades to "does not persist"
  rather than breaking the page.
- Keyed by **news item id**, not event id, so saving from the map and from the feed refer to the
  same story. One story that places several events lights up all of them, which is the intent.
- The saved flag is written onto the GeoJSON features client-side rather than driven by a MapLibre
  filter, so toggling the shortlist repaints instantly with no refetch.
- **`/api/events` gained `cluster=off`.** A cluster cannot be narrowed to a shortlist in the
  browser, so the saved-only view asks for plain points instead. `news_item_id` is now exposed on
  point features to make the shortlist addressable at all.
- The feed's saved view lists items from the local note even if the API no longer serves them, so a
  saved item never silently vanishes.
- Both the panel control and the status line report real counts ("4 saved of 312 events here"),
  since the map filter is applied client-side and the server's total would otherwise mislead.

## 33. The LLM pipeline could never keep up with ingest

Reported symptom: the site still showed mostly Ukrainian and Russian text hours after translation
shipped. The cause was throughput, not translation.

Beat fired **one** `llm_extract_batch` and **one** `translate_batch` every 120 s. At 8 items per
batch that is ~240 items/hour, against a measured ingest rate of ~230 items/hour with 210 sources
enabled. The pipeline had no headroom at all: a backlog could never be worked off, and the newest
— most visible — items were exactly the ones left untranslated.

Worse, the queue those tasks sat on was jammed. `dispatch_polls` fanned out up to 200 poll tasks
every 60 s with no regard for whether the previous round had drained; with 210 sources and a few
slow feeds it had built a **1,123-task backlog** on the `default` queue. `dispatch_llm` was
registered and being scheduled on time, but never reached a worker slot.

Three changes:

- **`tasks.dispatch_llm`** replaces the two fixed timers. It tops the `llm` queue up to
  `LLM_QUEUE_TARGET` (12) every 30 s, splitting the free slots between extraction and translation
  in proportion to what is actually pending, and never starving whichever has less.
- **`dispatch_polls` is bounded.** It backs off entirely once the `default` queue reaches
  `POLL_QUEUE_MAX` (120) and otherwise dispatches only up to the remaining headroom.
- **The control plane is isolated.** Both dispatchers now run on their own `control` queue, so a
  data-plane flood can never starve the tasks that decide what everything else does. The default
  worker consumes `control,default` at concurrency 4 (polling is I/O-bound; 2 was too low for 210
  sources), and `docker-compose.yml` gained the dedicated `llmworker` at concurrency 4 — the
  measured saturation point of the proxy (§18) — which had previously existed only in the
  load-test overlay.

Measured after the change: the `default` queue drains to 0 and stays there, the `llm` queue holds
at its target of 12, and the newest 50 items went from 34 to 47 ready within six minutes; the front
page is now served entirely in English. The remaining ~10k historical backlog clears at roughly
3,500 items/hour.

The general lesson, which applies beyond this feature: a fixed-rate timer is not a scheduler. Any
producer that can outpace its consumer needs the consumer's depth in the loop.

## 34. Verification records were confirming frontline changes

Reported symptom: isolated red circles scattered across the map, some filled and some cut out of
Russian-held area, with no obvious reason. One example carried `claim #172 · confirmed`.

The circles are §14.3's fallback shape for a confirmed claim ("the claim's settlement `boundary`,
or a 2 km point buffer"). Because the gazetteer is built from GeoNames, which ships no settlement
polygons, `gazetteer.boundary` is **0 of 36,184 rows** — so the buffer is not a fallback, it is
always the shape used. That alone is cosmetic. The real problem was how many claims existed:

**961 of 987 confirmed claims had been confirmed by `rule:geoproof`, and every one of them rested
on evidence with no `claimed_by`** — 7,578 of 7,593 proof links were bulk-ingested GeoConfirmed
records. The map was asserting control over roughly **12,400 km²** on that basis.

This is the same misreading already recorded in §28 for the beneficiary axis, applied here to the
rule engine and missed at the time: a GeoConfirmed placemark verifies *a thing* — a destroyed
vehicle, an air-defence site, a satellite image of a factory — not who holds the ground. §13's rule
3.2 is about a proof of a *position*; §11's prompt defines a `geolocation_proof` as an item that
"presents verified imagery/coordinates proving a position". Bulk verification records are neither.

**Rule:** a `geolocation_proof` counts toward confirmation only if it names a side
(`claimed_by` set). Such an event may still open no claim, still appears on the map, and still
attaches to a nearby claim for the audit trail — it simply carries neither rule 3.2 on its own nor
an independent perspective under rule 3.1, because it asserts nothing about control. LLM-extracted
proofs from news text carry `claimed_by`; bulk records do not.

Effect: confirmed claims fell from **987 to 36** (26 by corroboration, 10 by assessed geo-proof).
The 961 affected claims were reset to `pending` and re-evaluated rather than marked `reverted`,
since nothing was debunked — the rule changed.

Three related fixes came out of it:

- **`CLAIM_APPLY_KM`** (default 2.0) replaces the hardcoded 2 km buffer, so the shape is tunable
  while `gazetteer.boundary` stays empty.
- **Revert attribution is honest.** Every revert used to be recorded as `rule:debunk`, which
  misleads the admin's evidence view. A claim that lost a *debunked* proof is now
  `rule:debunk`; one whose evidence an admin deactivated is `rule:evidence_retracted`; one that
  simply no longer meets the bar is `rule:insufficient_evidence`.
- **The baseline cutoff uses `valid_at`, not `fetched_at`.** Whether a baseline already absorbed a
  claim depends on the instant the baseline *depicts*, not when it was downloaded. Using
  `fetched_at` made the answer depend on write order — the history backfill moved it around — and
  dropped claims resolved after the newest snapshot was published but before it was fetched.

The circles that remain are the genuine article: changes corroborated since DeepStateMap's last
published snapshot, which §14.3 explicitly wants shown as the fast path ahead of the baselines.
The legend now says so, since an unexplained circle is indistinguishable from a glitch.

## 35. Daily digests were manufacturing corroboration

Reported symptom: a *shelling* report appeared to have given Russia a red control circle, in several
places — "some systematic bug here". Correct instinct, wrong suspect.

The rule engine was clean on shelling: no `shelling` or `deep_strike` event is linked to any claim,
and no confirmed claim rested on one. The clicked marker simply sat inside a 2 km disc belonging to
a *different*, nearby claim.

That claim, and dozens like it, came from **daily situation summaries**. The corpus is full of them:
the Ukrainian General Staff's "231 clashes on the front in 24 hours" (36 settlements named), the
Russian MoD's daily bulletin (20), "Сводка основных боевых действий утром 24 августа" (31),
"Изменения на карте за вчера", "Daily Tactical Update Day 1119". Extracting one event per named
place is correct for the map. Treating each of those events as a *report that this settlement
changed hands* is not — and once two such digests from different perspective classes both listed
the same town, §13's corroboration rule fired and confirmed a control change **neither of them
asserts**.

The corpus splits cleanly: focused reports name 1–5 settlements, digests 6–36.

**Rule:** `CLAIM_MAX_LOCATIONS` (default 5). An item that names more than this many settlements is
a roundup. Its events still appear on the map and in the feed, but they neither open a claim nor
count as evidence for one. Applied to both intake and re-evaluation, so existing links are
re-judged rather than grandfathered.

Combined with §34, confirmed claims fell **987 → 36 → 5**. Each survivor was checked by hand: all
five are corroborated by two distinct perspectives from focused reports (2–4 usable links each),
which is exactly what rule 3.1 is for.

One more attribution fix fell out of it: a claim that stays confirmed *for a different reason* than
recorded now has `resolved_by` updated (three of the five still read `rule:geoproof` after the
proofs stopped counting). An `admin:` attribution is never overwritten.

The general shape of both §34 and §35 is the same mistake twice: the rule engine trusted an event's
*type* without asking what the underlying item actually asserted. A GeoConfirmed placemark typed
`geolocation_proof` verifies a thing, not control; a General Staff digest typed
`frontline_advance` at thirty settlements asserts a change at none of them.

## 36. News rail: the two decisions plans/NEWS_RAIL.md deferred to implementation

The rail itself landed as specified (`placement=` on `/api/news`, migration
`0006_news_placement_index`, mirrored right-rail panel). Two choices were left open in the plan:

**Index (§3.1):** verified with `EXPLAIN ANALYZE` against a live corpus — the unplaced query
runs as a backward PK scan with an index-only anti-join probe on
`ix_extracted_events_item_placed` (0.1 ms, zero heap fetches). The index stays.

**Timebar vs. rail (§4):** the simple version won. The timebar keeps its centred
`min(96vw,720px)`; on viewports narrower than ~1430px its right end can slide under the rail,
and its higher z-index (6 vs 5) means it wins that corner. What was *not* acceptable was
burying MapLibre's own controls: the zoom, scale and attribution controls at `top-right` /
`bottom-right` sat exactly under the rail column, so `style.css` shifts both control corners
left of the rail (and back to the edge below 640px, where the rail is hidden).

One fix beyond the plan: a collapsed `.panel` used to keep its `flex: 1 1 auto` and stretch
into a tall empty box over the map — invisible before because nothing started collapsed, but
the rail starts collapsed on widths ≤1100px. `.panel:has(> .panel-body[hidden])` now stops the
growth; this also fixes the same artefact when a reader collapses the legend.

## 37. Map symbols: size stops and what the plan left to tuning

Per-type marker shapes landed as specified in `plans/MAP_SYMBOLS.md` (SDF sprites generated at
boot from shared SVG paths, symbol layer, circle fallback). Two values differ from the plan's
illustrative numbers:

**`icon-size` stops (§2.2):** the plan sketched 0.30/0.42/0.56 on the assumption they would be
tuned. Shipped: **0.5/0.75/1.0** (zoom 4/8/12) on the 64px-at-`pixelRatio:2` sprites. The
shapes are drawn with ~8px of box padding, so the plan's stops rendered the actual silhouette
*smaller* than the old circle diameters (10/16/22px) — the opposite of the plan's own "a touch
larger" requirement. The shipped stops give ~12/18/24px of silhouette.

**`styleimagemissing` (§2.4):** implemented as a rebuild-all — `installEventImages()` skips
images that already exist (`map.hasImage`), so re-running it for any missing `shape-*` id is
idempotent and simpler than per-id regeneration.

One §4 nuance: `shapeIconHTML()` falls back to the unicode glyph span for a type with no shape
entry, so a server-added event type degrades to the old rendering in the legend/filters instead
of an empty icon; the `test_config_exposes_client_contract` key-set assertion is what actually
flags the drift.

## 38. Source reliability tiers re-graded: verification, not officialdom

The original `tier_of()` in `scripts/seed_sources.py` put every catalogue row typed
`Government` in tier 1 — which graded the Russian MoD (capture claims "premature by days or
weeks", loss figures routinely inflated) as *more* reliable than BBC Verify or Reuters, and
let its assertions both pass the default tier-1 news filter and count as confirmation-grade
evidence in `rules.py`. Re-graded on the principle that tier 1 means "verifies rather than
asserts":

- **Governments are tier 2 across the board** — belligerent ministries and allied ones alike
  are partisan primary sources: quotable as claims, never as verification. Ukrainian General
  Staff moved 1→2 for the same reason (unverifiable claims, systematic under-reporting of
  withdrawals). NASA FIRMS keeps tier 1 by name override: instrument data, not statements.
- **Name overrides for track-record cases:** Russian MoD, TASS, RIA Novosti, SolovievLive,
  Readovka → 3; BBC, Reuters, AFP, DW, RFE/RL, Meduza, Mediazona → 1.
- **The note-pattern regex was matching praise as guilt:** plain `unverified` demoted Meduza
  for *labelling* unverified claims, `no independent` demoted NATO for doing "no independent
  battlefield reporting". Patterns are now phrase-specific (`amplif… unverified`,
  `systematically unverified`, `wholly partisan`, `never as evidence`), and a new tier-2 cap
  (`no independent verification`) grades honest pass-throughs (WarTranslated, CEPA/ECFR,
  aggregator maps) without branding them disinformation. StopFake and Necro Mancer are
  name-rescued from false positives.

Catalogue distribution went 232/250/56 (T1/T2/T3) → 127/377/34. Applied to the live DB with
`--no-enable-starters` (metadata refresh only, nobody's enabled flag touched). Existing claims
confirmed while the MoD counted as tier-1 evidence were not re-adjudicated.

Post-plan addition (user request): **stacked markers page through one popup**. With shapes the
overlap problem became visible — several events at one settlement centroid stack their icons and
only the top one was clickable. `onEventClick` now takes *all* features under the click (deduped
by event id — GeoJSON sources are tiled internally and can report a feature twice), sorts them
newest-first, and `openEventPopup()` renders a `‹ 2/13 ›` pager row that re-renders and re-wires
the popup per step, following each item's own coordinates. `showOnMap()` feeds the same function
a single pseudo-feature, which as a side effect fixed its previously-unwired star/original
buttons.

## 39. Panels start collapsed; each remembers its state per browser

Post-plan change (user request): Filters, Legend and the General-news rail now all start
**collapsed** by default, replacing NEWS_RAIL.md's width rule (rail collapsed only between
641–1100px via a one-shot `matchMedia` check, everything else open). Each toggle click is
persisted per panel to `localStorage` (`ukraine-aggregator:panels:v1`, same try/catch treatment
as the saved-items shortlist), so a returning browser reopens exactly the panels it last had
open. The static HTML ships `aria-expanded="false"` + `hidden` so the default case never
flashes open before the script runs; the stored state is applied in `buildFilters()`. The
stored choice wins over viewport width — someone who opens the rail on a narrow window gets it
back open there too.
