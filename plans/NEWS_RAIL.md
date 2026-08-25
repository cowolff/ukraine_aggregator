# News Rail — general (unplaced) news beside the map

Status: **implemented** (2026-08-25). The two decisions deferred below (§3.1 index, §4 timebar)
are resolved and logged in `plans/DEVIATIONS.md` §36.

## 1. Goal

A feed panel on the **right edge of the map view** showing *general* war news — items that have
no clear location and therefore never appear as a marker. Today those items are only reachable by
switching to the Feed tab, so the map view silently hides exactly the class of reporting the
pipeline was built to keep (daily General Staff summaries, drone/missile-count overnight reports,
frontline overview posts, long-form reports):

```
Ukrainska Pravda — War section                     General Staff of the AFU — Telegram
At night, occupiers launched 150 drones and two    Operational information as of 22:00 on
missiles at Ukraine; air defense neutralized 124   23.08.2026 regarding the Russian invasion

WarGonzo (Semyon Pegov)
Frontline report for the morning of August 24
△ Fighting reported · Favours Russia
1 d ago · confidence 0.85 · source tier 3
```

The rail mirrors the left rail (filters/legend) visually: same `.panel` chrome, collapsible,
its own scroll. It reuses the existing feed row rendering — perspective pill, source name,
relative time, translation *original* toggle, ★ shortlist, event-type chips — so a story looks
the same in the rail and on the Feed tab.

## 2. What counts as "no clear location" (core semantic)

**An item is *unplaced* iff no visible extracted event of it has a geometry**
(`NOT EXISTS (SELECT 1 FROM extracted_events e WHERE e.news_item_id = n.id AND e.visible AND e.geom IS NOT NULL)`).

Consequences, all intentional:

- Items whose extraction produced only ungeocodable events (`△ Fighting reported (unplaced)`)
  **are** in the rail — the WarGonzo example above is exactly this shape.
- Items with **zero** events (LLM said "nothing extractable", `llm_status = skipped/failed`)
  are in the rail.
- Items still `pending`/`processing` are in the rail too. The predicate is deterministic and
  does not depend on pipeline state, so an LLM-proxy outage never empties the rail. The cost:
  an item that later geocodes migrates out of the rail on the next refresh. Extraction normally
  completes within minutes, so churn is a handful of items near the top; acceptable.
- A mixed item (one placed + one unplaced event) is **not** in the rail — it is already on the map.
- Blackout interaction needs no new rule: the existing `/api/news` clause already passes unplaced
  items unconditionally, and an item whose only placed events sit inside an active blackout zone
  stays hidden everywhere (it *has* a location; we are masking it, not reclassifying it).

## 3. API change — `app/api/news.py`

Extend `GET /api/news` rather than adding an endpoint: the rail is the existing feed with one
more filter, and every behaviour (cursor pagination, perspective filter, blackout masking,
translation fields, Redis+ETag caching) must stay identical.

New query params:

| Param | Values | Meaning |
|---|---|---|
| `placement` | `placed` \| `unplaced` | filter by the §2 predicate; absent = both (today's behaviour) |
| `to` | ISO-8601 | `COALESCE(n.published_at, n.fetched_at) <= :to` — for time travel (§5.4) |
| `from` | ISO-8601 | `COALESCE(n.published_at, n.fetched_at) >= :from` |

Implementation notes:

- Validate `placement` against the two literals, `from`/`to` with the same datetime parsing
  `app/api/events.py` uses; `bad_request()` otherwise.
- Add all three to the `params` dict **even when unset** — `cache.params_key` hashes that dict,
  and omitting them would alias cache entries between old- and new-shape requests.
- `placement=unplaced` compiles to the `NOT EXISTS` in §2; `placed` to the mirrored `EXISTS`.
  Note the predicate uses `e.visible` and **raw `geom IS NOT NULL`** (no blackout condition),
  per §2.
- Ordering stays `n.id DESC` with the existing cursor. Caveat, documented in the endpoint
  docstring: backfilled history has large ids with old `published_at`, so `to=` filters rows but
  does not reorder them. That matches how the Feed tab already behaves and keeps the cursor
  contract intact.

### 3.1 Index

The rail's steady-state query is "newest N ids with no placed event". `extracted_events` already
has `ix_extracted_events_occurred_geom (geom) WHERE geom IS NOT NULL` but nothing keyed by
`news_item_id` for the anti-join probe. Add in a new migration (`0006_news_placement_index`):

```python
Index("ix_extracted_events_item_placed", "news_item_id",
      postgresql_where=text("visible AND geom IS NOT NULL"))
```

The anti-join then probes a small partial index per candidate row. Verify with `EXPLAIN` on a
seeded DB before merging; if the FK column already carries an index that plans well, record that
in `plans/DEVIATIONS.md` and drop this step.

## 4. Frontend — layout (`index.html`, `style.css`)

Add a right-hand column inside `#view-map`, symmetric to `.left-rail`:

```html
<div class="right-rail">
  <aside class="panel news-rail" id="news-rail">
    <button class="panel-toggle" id="news-rail-toggle" aria-expanded="true">General news</button>
    <div class="panel-body" id="news-rail-body">
      <p class="seg-help">Reports without a clear location — they never get a map marker.</p>
      <ol class="feed rail-feed" id="rail-feed"></ol>
      <div class="feed-end" id="rail-end"></div>
    </div>
  </aside>
</div>
```

CSS:

- `.right-rail` clones `.left-rail` geometry mirrored: `position:absolute; top:.7rem; right:.7rem;
  bottom:1.8rem; width:min(88vw,340px); pointer-events:none; z-index:5`. Keep `bottom:1.8rem` so
  the `.attrib` credit line (bottom-right, z-index 4) stays visible below it.
- `.news-rail{flex:1 1 auto;min-height:2.4rem}` so the panel fills the column and
  `.panel-body` scrolls (the panel chrome already handles overflow + collapse).
- `.rail-feed` reuses `.feed` list styling with tighter metrics: no max-width/centering,
  `gap:.5rem`, `font-size` one notch down, snippets clamped to 3 lines
  (`display:-webkit-box; -webkit-line-clamp:3`).
- The centred `.timebar` is `width:min(96vw,720px)`; on narrow-but-desktop widths it can slide
  under the rail. Cap it instead at `min(96vw, calc(100vw - 2*370px))` when the rail is open, or
  simply accept overlap since both are `pointer-events:auto` panels — decide visually during
  implementation; note the choice in DEVIATIONS if the simple version wins.
- Responsive: in the existing `@media (max-width:640px)` block, `display:none` the right rail —
  phones already have the Feed tab. Between 641–1100px start the panel **collapsed**
  (`aria-expanded=false`, JS reads `matchMedia` once at init) so the map is not squeezed by two
  open rails.

## 5. Frontend — behaviour (`app.js`)

New section `/* --------- news rail */`, plus three `State` fields:
`railCursor`, `railLoading`, `railExhausted` (same trio the Feed tab uses).

### 5.1 Fetching

`railURL(cursor)` = `/api/news?placement=unplaced&limit=20` + cursor + the time window (§5.4).
`loadRail({reset})` is `loadFeed()`'s twin against `#rail-feed`/`#rail-end`. Infinite scroll on
`#news-rail-body` scroll (same 300px threshold as `#view-feed`).

### 5.2 Rendering — reuse `feedRow()`

`feedRow(item)` is used as-is, with two small parameterisations rather than a fork:

- `feedRow(item, {compact:true})` suppresses the `(unplaced)` suffix on chips — in this rail
  everything is unplaced, the suffix is noise — and skips the "Show on map" button branch
  (never true here anyway, since nothing has a placed event).
- Star, `original` toggle, pill, chips, snippet all come along for free. `Saved.toggle` already
  keys on item id, so shortlisting from the rail and the Feed tab is the same list;
  `renderFeedSavedState()` gains a sibling `renderRailSavedState()` (or generalise it to take a
  container id) so toggling in one place repaints the other.

Metadata line: the examples show `1 d ago · confidence 0.85 · source tier 3`. `relativeTime()`
exists; confidence comes from `item.events[].confidence` (show the max when events exist);
tier from `item.source.reliability_tier`. Render as a `.when`-styled suffix in the `.head` row —
same faithful-labelling policy as everywhere else (perspective from the source record, never
implied).

### 5.3 Live refresh without scroll-jank

Hook into the existing poll loop (`State.timer`, `app.js:1206`): each tick, alongside
`refreshEvents()`, fetch page 1 of `railURL(null)` and **prepend only items with
`id > newest rendered id`**; never reset the list under the reader. A full `reset` happens only
at init and on time-travel transitions (§5.4). Items that got geocoded since the last
poll simply stop appearing in *new* pages (§2 churn); stale rows above the fold are harmless
and disappear on the next reset.

### 5.4 Time travel

The rail must not show today's news over a 2025 map. Mirror `eventsURL()`'s windowing: when
`isHistorical()`, request `from = at − rangeHours`, `to = at`, and `loadRail({reset:true})` on
every scrub step / LIVE return (piggyback wherever `refreshEvents()` is called on scrub). While
historical, the §5.3 prepend polling is already suppressed by the existing
`if (isHistorical()) return;` guard.

### 5.5 Empty/error states

`#rail-end` mirrors the feed's three states ("Nothing here for this window.", "End.",
"Unavailable (…)"). If the very first load 4xx/5xxes, keep the panel with the error line —
do not hide it, or nobody will ever find the feature.

## 6. What does **not** change

- No new model, no pipeline change — the rail is a read-side view over data already ingested.
- Admin: news moderation (hide/delete/re-extract) already covers rail items.
- `/api/config`: nothing new required; rail page size is a frontend constant.
- Feed tab behaviour is untouched (no default `placement` param).

## 7. Tests

`tests/test_api.py` (extend the existing `/api/news` class; fixtures at `test_api.py:344` already
create placed events):

1. `placement=unplaced` returns items with no events and items with only geom-less events;
   excludes items with a placed event; excludes mixed items.
2. `placement=placed` is the exact complement; no `placement` returns the union (regression).
3. `placement=martian` → 400; `to=notadate` → 400.
4. `from`/`to` window on `COALESCE(published_at, fetched_at)`.
5. Blackout: an item whose only placed event is inside an active zone appears in **neither**
   `placement` bucket (it stays masked, per §2).
6. An event with `visible=false` and a geom does not make its item "placed".
7. Cache-key isolation: request without `placement`, then with — second must not serve the
   first's cached body (this is the §3 params-dict bug trap).

Frontend is not unit-tested in this repo (no build step); manual checklist in the PR:
rail scrolls independently, collapse persists visually, star sync with Feed tab, original
toggle, scrub to a past date empties/refills the rail, 640px hides it.

## 8. Documentation

- README: add the rail to the feature list ("General news — reports with no clear location —
  in a panel beside the map") and the two new params to the `/api/news` row of the API table.
- `plans/DEVIATIONS.md`: whatever §3.1 EXPLAIN and §4 timebar decisions turn out to be.

## 9. Build order

Each step ends green and deployable:

1. **API + migration** — `placement`/`from`/`to` params, index, tests 1–7.
2. **Rail markup + fetch/render** — §4 + §5.1/5.2/5.5; static rail, infinite scroll.
3. **Live refresh + time travel** — §5.3/5.4.
4. **Responsive polish + docs** — §4 breakpoints, README, DEVIATIONS.

## 10. Risks

- **Rail churn** (§2): pending items that later geocode vanish from the rail. Accepted; if it
  reads badly in practice, tighten the predicate to `llm_status IN ('done','skipped','failed')`
  behind the same `placement` param — one clause, no API shape change.
- **Anti-join cost** on a large `news_items` table: mitigated by §3.1's partial index and the
  existing Redis response cache (`api_cache_ttl_s`); the query shape is identical to the blackout
  clause the endpoint already runs.
- **Screen budget**: three panels (filters/legend left, news right, timebar bottom) is a lot of
  chrome. The rail is collapsible and starts collapsed on medium widths; if it still crowds,
  the fallback is collapsing the legend by default instead — a one-line change.

---

## Addendum (2026-08-25): source-quality filter and reporting-time order

Two follow-ups requested after the rail shipped, both implemented:

**Quality filter.** The rail gets a "Source quality" select — *Tier 1 — most reliable only*
(the default), *Tiers 1–2*, *All tiers*. Server-side as `max_tier=` on `/api/news`
(`s.reliability_tier <= :max_tier`, 1 = most reliable, catalogue currently uses 1–3). The tier
shown on every row comes from the source record, so the filter is as deterministic as the label.

**Reporting-time order.** The rail previously inherited the feed's `id DESC` (ingest order),
which floats backfilled history — large ids, old dates — to the top. `/api/news` now takes
`order=published`: `COALESCE(published_at, fetched_at) DESC, id DESC`, with a keyset cursor
`<id>@<iso>` issued in `next_cursor` and replayed verbatim by the client (row-comparison
`(ts, id) < (:ts, :id)` keeps ties stable). The default `order=id` and its plain integer cursor
are unchanged — the Feed tab and the §15 contract are untouched. Migration
`0008_news_published_order` adds an expression index on the coalesced timestamp;
`ix_news_items_published_at` cannot serve it because bare-column order misplaces NULL
`published_at` rows.

The rail's live-refresh prepend keys on the top row's timestamp (plus an already-rendered id
set for exact ties) instead of "id greater than newest", for the same backfill reason.
