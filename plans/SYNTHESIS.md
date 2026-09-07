# Synthesis — cross-source event reports for the general news rail

Status: **implemented** (2026-08-25). Numbers marked *(tune)* are starting points, not
measurements. As-built deltas from the sketch below, all minor:

- The clustering pass processes evidence-only types (debunks, geolocations) *after* the founding
  types, so a debunk older than the reports it disputes joins the cluster they found in the same
  tick instead of waiting for the next one.
- Closed reports that never armed (singleton/duo clusters that never became a story) are pruned
  after 7 days — housekeeping the sketch did not specify.
- The dispatcher's fourth kind is named `synthesize_reports` (task
  `tasks.synthesize_reports_batch`); the clustering beat task is `tasks.cluster_synthesis`,
  offset 90 s from `evaluate_claims`.
- Not a pinned block (§7 as sketched): syntheses are **threaded into the timeline** of both the
  general news rail and the Feed tab, as tinted rows keyed on `last_reported_at` — each inserted
  before the first older row, exact in the rail (published order), best-effort in the ingest-
  ordered feed. One fetch feeds both lists. They step aside when the feed is filtered (search,
  perspective, ★ shortlist) — a cross-source synthesis matches neither a search term nor a
  single perspective.

## 1. Goal

When many independent sources report on what is evidently **the same incident** — extracted events
of the same type landing within a few kilometres of each other inside a short time window — the
map today shows a stack of markers and the reader has to page through ‹ › popups to discover that
five outlets, across perspectives, are describing one strike. Nobody writes the sentence *"this is
one event, reported five times, and here is how much you should trust it."*

This feature writes that sentence. A **synthesized report** is a pipeline-generated item that:

1. is triggered by *location alignment*: ≥ `SYNTH_MIN_SOURCES` distinct sources whose events of the
   same type cluster within `SYNTH_JOIN_KM`, inside a rolling `SYNTH_WINDOW_HOURS` window;
2. carries an LLM-written English summary that merges the accounts and **names the disagreements**
   (casualty counts, attribution, weapon type) instead of averaging them away;
3. carries a **deterministic credibility verdict** derived from the member sources' reliability
   tiers and perspective classes — computed in code, never by the LLM, exactly like perspective
   labelling today — so the reader is told *whether to believe it* and why;
4. appears at the top of the **general news rail**. The members are placed (they have markers);
   the synthesis is a meta-report *about* those markers, and the rail is where map-adjacent prose
   lives. Clicking it flies the map to the cluster.

This is deliberately the editorial mirror of the rules engine: `FrontlineClaim`/`EvidenceLink`
already correlate events by `ST_DWithin` proximity and confirm on ≥2 perspective classes — but
only for frontline event types, and their output is geometry, not prose. Synthesis reuses the
same correlation idiom for *all* placed event types and outputs a paragraph.

## 2. Data model

Two tables, mirroring `FrontlineClaim` + `EvidenceLink` (`app/models/frontline.py`):

```python
class SynthesizedReport(Base):
    __tablename__ = "synthesized_reports"
    id: int
    event_type: str            # from EVENT_TYPES; clusters never mix types (v1, see §9)
    geom: Geometry("POINT", srid=4326)   # running centroid of member events
    gazetteer_id: int | None   # modal gazetteer entry of members, for a display name
    place_name: str | None     # denormalised display name ("Kharkiv area")
    status: str                # open | closed  — whether new events may still join (§3)
    llm_status: str            # pending | processing | done | failed  (LLM_STATUSES minus skipped)
    llm_claimed_at, llm_attempts                       # same claim machinery as news_items
    headline_en: str | None    # LLM output
    summary_en: str | None     # LLM output
    disagreements_en: str | None  # LLM output, nullable — "counts differ: 3 (UA) vs 12 (RU)"
    credibility: str           # confirmed | corroborated | reported | unverified  (§4, code-derived)
    cred_meta: JSONType        # {"sources": 5, "classes": ["ukrainian","russian"], "best_tier": 1,
                               #  "tiers": {"1": 2, "2": 2, "3": 1}} — the badge's audit trail
    member_count: int          # denormalised, drives refresh hysteresis (§5)
    synthesized_member_count: int  # member_count at the time of the last completed LLM pass
    first_reported_at, last_reported_at: datetime      # min/max member occurred_at — the rail's time axis
    created_at, updated_at
    visible: bool              # admin hide, default True

class SynthesisMember(Base):
    __tablename__ = "synthesis_members"
    id: int
    report_id: FK synthesized_reports, ondelete CASCADE
    event_id:  FK extracted_events,   ondelete CASCADE
    # UNIQUE (report_id, event_id); index on event_id for the "already a member?" probe
```

Indexes: GiST on `geom`; `(status, event_type)` partial `WHERE status = 'open'` for the join probe;
`(llm_status)` partial `WHERE llm_status = 'pending'` for the dispatcher count;
`(last_reported_at)` for the rail window query.

Both classes must be exported from `app/models/__init__.py` and appended to `TABLES` in
`tests/conftest.py` (truncation order: members before reports before events).

Why link **events**, not news items: the trigger is spatial, and geometry lives on
`extracted_events.geom`. The member's news item, source, tier and perspective are one join away
(the exact join `_evidence_rows` in `app/services/rules.py` already writes).

## 3. Clustering — incremental anchor-join, not batch DBSCAN

No `ST_ClusterDBSCAN` snapshots. Copy the rules engine's incremental idiom (`nearby_claim`,
`app/services/rules.py:165`): each new event either **joins** the nearest open report or
**founds** one. This is idempotent, cheap per beat tick, and never re-clusters history.

A new beat task `tasks.cluster_synthesis` (queue `default`, every 300 s, offset from
`evaluate_claims`) scans events that are candidates and not yet members:

```sql
SELECT e.id … FROM extracted_events e
JOIN news_items n ON n.id = e.news_item_id
JOIN sources s ON s.id = n.source_id
WHERE e.geom IS NOT NULL AND e.visible AND n.visible
  AND e.occurred_at > now() - interval :window
  AND e.confidence >= :low_confidence_floor
  AND NOT EXISTS (SELECT 1 FROM synthesis_members m WHERE m.event_id = e.id)
```

For each candidate, with the digest guard applied in Python (below):

1. **Join**: nearest `synthesized_reports` row with `status='open'`, same `event_type`,
   `ST_DWithin(geography, :SYNTH_JOIN_KM * 1000)`, and `last_reported_at` within
   `SYNTH_WINDOW_HOURS` of the event's `occurred_at`. Insert member; update centroid
   (weighted incremental mean is fine at these radii), `member_count`, `last_reported_at`.
2. **Found**: no joinable report → insert `status='open', llm_status=NULL, credibility=NULL`
   report with this event as sole member. Founding is silent — nothing renders until the
   threshold in §5 arms the LLM stage.

**Closing**: the same task closes any open report with
`last_reported_at < now() - SYNTH_WINDOW_HOURS`. Closed reports never accept members; a fresh
burst of reporting about the same place founds a *new* report. Recurrent shelling of one city
therefore yields roughly one synthesis per active day — intended, since "it happened again today"
is a new story.

**Guards, all inherited from the rules engine — the false-corroboration lessons are already paid
for** (`plans/DEVIATIONS.md` §35):

- **Digest suppression**: events from items naming > `claim_max_locations` settlements
  (`is_digest`, `item_location_counts`) never join or found. Two General Staff overnight roundups
  both mentioning Kupiansk are not two reports of one Kupiansk event.
- **Confidence floor**: `confidence >= settings.low_confidence_floor` (0.4).
- **Distinct sources**: one source posting five updates is *one* source. All counting in §4/§5 is
  over `DISTINCT source_id` (and events from the same *news item* are trivially one source).
- `debunk` and `geolocation` events join clusters as evidence but never found one — a lone
  geolocation is already handled by the claims machinery, and a debunk with nothing to debunk is
  noise. A debunk joining an existing cluster forces re-synthesis (§5) so the summary can say so.

## 4. Credibility — deterministic, tier- and perspective-weighted

The verdict is computed in code from the member set, in the spirit of `_confirmation`
(`app/services/rules.py:460`), reusing `PERSPECTIVE_CLASS` (neutral collapses into western — 3
classes). Evaluated top-down, first match wins:

| Verdict | Condition (over distinct sources) | Rendered as |
|---|---|---|
| `confirmed` | a member is a **verified geolocation** from a tier-1 source, or ≥2 perspective classes each with a tier-≤2 source | ● "Confirmed — independent perspectives agree" |
| `corroborated` | ≥2 perspective classes (any tiers), or ≥3 sources with at least one tier-1 | ◐ "Corroborated — multiple outlets, one side" if single-class |
| `reported` | threshold met, single perspective class, best tier ≤2 | ○ "Reported — not independently confirmed" |
| `unverified` | threshold met only by tier-3 sources | ◌ "Unverified — low-reliability channels only" |

An **active** debunk member caps the verdict at `unverified` and sets a `disputed` flag in
`cred_meta`, surfaced as its own badge.

`cred_meta` stores the inputs (source count, class list, tier histogram, best tier) so the badge
is auditable and the UI can render the tooltip *"5 sources: 2× tier 1, 2× tier 2, 1× tier 3;
Ukrainian + Russian perspectives"* without re-deriving anything.

The LLM is *told* the verdict and the per-member tiers and instructed to write in register —
"visually confirmed", "so far reported only by low-reliability Telegram channels" — but its prose
can only ever echo the code's verdict, never invent one. Same principle as perspective labels:
anything trust-related is deterministic and auditable.

## 5. LLM stage — fourth kind in the dispatcher

Threshold to arm: an open report becomes `llm_status='pending'` when **distinct sources ≥
`SYNTH_MIN_SOURCES`** (default 3 *(tune)*). Re-synthesis: a `done` report flips back to `pending`
when `member_count - synthesized_member_count >= SYNTH_REFRESH_MIN_NEW` (default 3 *(tune)*), when
a debunk joins, or when the report closes with new members since the last pass (one final pass so
the summary reflects the complete evidence). Hysteresis keeps a hot cluster from burning a prompt
per new repost.

Plumbing, all by existing pattern:

- `celery_worker/tasks/synthesize.py`, task `tasks.synthesize_reports_batch`, modelled line-for-line
  on `summarize.py`: claim with `FOR UPDATE SKIP LOCKED`, commit before the network call, release
  what didn't fit, failure → `pending` with attempt cap.
- `dispatch_llm` (`celery_worker/tasks/dispatch.py`): add `synthesize` as a fourth kind in the
  single pending-counts query, the `tasks` dict and `_split_slots`. Synthesis volume is tiny
  (clusters, not items), so it will mostly take the leftover slot — but it must be in the split so
  it can't be starved by a translation backlog.
- Module added to `celery_app.py` `include`; routed to `llm` via explicit
  `apply_async(queue=LLM_QUEUE)` like `summarize_batch`.

Prompt: a new module constant `SYNTHESIS_PROMPT` in `app/services/llm.py` (byte-stable, cached),
`{"items":[{"idx":…}]}` in / indexed JSON out via `chat_json`, like the other three. One *item* here
is one **report** with its members inlined:

```json
{"idx": 0, "event_type": "shelling", "place": "Kherson", "verdict": "corroborated",
 "reports": [
   {"source": "Suspilne", "perspective": "ukrainian", "tier": 1,
    "at": "2026-08-25T06:40Z", "title": "…", "summary": "…"},
   {"source": "Rybar", "perspective": "russian", "tier": 3, "at": "…", "title": "…", "summary": "…"}
 ]}
```

Members are fed as `title_en`/`summary_en` (fall back to originals — the model translates anyway),
best-tier-first, capped at `SYNTH_MAX_MEMBERS_IN_PROMPT` (12 *(tune)*) picked to maximise
perspective diversity; the prompt states "and N further similar reports". Output per idx:
`{"headline": …, "summary": …, "disagreements": … | null}`. Instructions: neutral register,
attribute every load-bearing claim to a perspective ("Ukrainian outlets say… Russian channels
claim…"), weight wording by tier, never resolve a numeric disagreement by picking a side — state
the spread in `disagreements`.

Ordering caveat: synthesis must run on *translated-or-original* text but must **not** wait for the
members' own summary stage — `summary_en` is best-effort input, not a dependency, or an LLM outage
in one stage would deadlock the other.

## 6. API — `GET /api/synthesis`

A separate endpoint, not a `placement` variant of `/api/news`: synthesized reports are few
(the threshold makes them rare by construction), so the rail pins the top-K rather than
interleaving into the news cursor — merging two keyset paginations for a handful of rows is
complexity with no reader benefit.

```
GET /api/synthesis?from=ISO&to=ISO&limit=20
→ {"items": [{
     "id": 17, "event_type": "shelling", "headline": "…", "summary": "…",
     "disagreements": "…" | null,
     "credibility": {"verdict": "corroborated", "sources": 5, "best_tier": 1,
                     "classes": ["ukrainian", "russian"], "tiers": {"1": 2, "2": 2, "3": 1},
                     "disputed": false},
     "place": "Kherson", "centroid": [lon, lat],
     "first_reported_at": "…", "last_reported_at": "…", "status": "open",
     "members": [{"source": "Suspilne", "perspective": "ukrainian", "tier": 1,
                  "url": "…", "title": "…", "news_item_id": 123}, …]
   }]}
```

- Windowed on `last_reported_at` (`from`/`to` from the scrubber) — time travel shows the syntheses
  of that moment, consistent with the rail.
- Only `llm_status='done' AND visible` rows; ordered `last_reported_at DESC`; `limit ≤ 50`.
- `cached_json("synthesis", params, …)` for Redis+ETag; the synthesize task and the clustering
  task `invalidate("api:synthesis")` on any change.
- **Blackout**: the report has a geometry, so apply the active-zone mask like `/api/events` does —
  a synthesis whose centroid is inside an active blackout zone is withheld at publish time
  (ingestion and clustering continue behind it, consistent with the blackout contract).

## 7. UI — pinned block in the general news rail

Top of `#news-rail`, above the unplaced feed, a visually distinct "Synthesized" block (its own
background tint — these are *our* words, not a source's, and must not masquerade as reporting):

```
◐ Corroborated — 5 sources, 2 perspectives                      shelling · Kherson · 2 h ago
Multiple outlets report artillery strikes on Kherson's
Dniprovskyi district this morning. Ukrainian sources say…
▸ Disagreements: casualty counts differ — 3 (UA officials) vs "over 10" (RU channels)
▸ Based on 5 reports  [Suspilne ①][UP ①][Rybar ③]…            → click flies map to cluster
```

- Badge glyph+colour by verdict; tooltip renders `cred_meta`. The tier chips reuse the existing
  source-tier styling; every member links to the original (attribution contract).
- The block follows the scrubber window like the rest of the rail; hidden when empty.
- Not in the ★ shortlist mechanism in v1 (shortlist keys are news-item ids; synthesis ids would
  collide — deferred).
- Rail tier filter (`#rail-tier`) does **not** filter the block: the verdict already encodes tier,
  and hiding a "unverified — tier-3 only" synthesis from a tier-1 reader would hide exactly the
  warning we built.
- Admin: list + hide/unhide + force re-synthesis on the News & events page; mutations audited.

## 8. Config knobs (`app/config.py`, env-overridable)

| Knob | Default *(tune)* | Meaning |
|---|---|---|
| `SYNTH_JOIN_KM` | 3.0 | member-to-report join radius ("very close alignment" — tighter than `CLAIM_JOIN_KM` 5.0, because same-type same-day within 3 km is one story, but two claims 5 km apart may legitimately be one frontline push and two towns) |
| `SYNTH_MIN_SOURCES` | 3 | distinct sources to arm synthesis |
| `SYNTH_WINDOW_HOURS` | 24 | join window / auto-close horizon |
| `SYNTH_REFRESH_MIN_NEW` | 3 | new members before re-synthesis |
| `SYNTH_MAX_MEMBERS_IN_PROMPT` | 12 | prompt cap, diversity-sampled |

## 9. Sequencing, tests, deferred

**Migration** `0010_synthesis` (tables, partial indexes, GiST), modelled on `0009_summaries`.

**Order of work**: models + migration → clustering task (pure SQL/Python, fully testable without
LLM) → credibility function (pure) → dispatcher + synthesize task (LLM monkeypatched) → API →
rail block → admin. Each step lands green on its own.

**Tests** (`tests/test_synthesis.py`, LLM faked per `test_summaries.py` convention):
clustering — join within radius, found beyond radius, type isolation, window close, digest
exclusion, same-source-counts-once, debunk-joins-but-never-founds; credibility — one case per
verdict row plus the debunk cap; dispatcher — fourth kind gets slots; API — window/ETag/blackout
masking; refresh hysteresis.

**Deferred, recorded here so DEVIATIONS.md has something to point at**:

- **Cross-type merging** (LLM labels one incident `strike` for one outlet, `explosion`-ish
  `shelling` for another → two parallel clusters). V1 accepts the split; a compatibility map
  (`strike`+`shelling`, `advance`+`fighting`) is the obvious v2 once real splits are observed.
- **Repost chains**: distinct Telegram channels reposting one origin count as distinct sources.
  The perspective-class requirement caps the damage — a single-class tier-3 chain can never rise
  above `unverified`, and no chain reaches `confirmed` alone — but true origin-tracing is out of
  scope.
- Shortlisting syntheses; showing a synthesis marker on the map itself (the members already mark
  the spot — a second marker double-counts); notification banners for `confirmed` verdicts.
