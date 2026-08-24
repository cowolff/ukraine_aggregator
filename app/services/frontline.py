"""Frontline snapshot builder (PLAN §14).

Always recomputed from scratch — never mutated incrementally — which is what makes a debunk-driven
revert automatic: a claim that lost its evidence simply stops contributing on the next build.
"""
from __future__ import annotations

import datetime as dt
import time

from sqlalchemy import select, text

from app.config import settings
from app.extensions import db, log
from app.models import FrontlineClaim, FrontlineSnapshot, UpstreamGeometry
from app.services.cache import clear_frontline_dirty, invalidate
from app.services.geo import EMPTY_MULTIPOLYGON

PROVIDERS = ("deepstate", "isw", "deepstate_grey")
UPSTREAM_STALE_HOURS = 72


def latest_upstream(provider: str, at: dt.datetime | None = None) -> UpstreamGeometry | None:
    """Newest layer for a provider, or the newest one valid at or before ``at``."""
    query = select(UpstreamGeometry).where(UpstreamGeometry.provider == provider)
    if at is not None:
        query = query.where(UpstreamGeometry.valid_at <= at)
    return db.session.execute(
        query.order_by(UpstreamGeometry.valid_at.desc()).limit(1)
    ).scalar_one_or_none()


def _fresh(row: UpstreamGeometry | None) -> bool:
    if row is None:
        return False
    age = dt.datetime.now(dt.timezone.utc) - row.fetched_at
    return age <= dt.timedelta(hours=UPSTREAM_STALE_HOURS)


def store_upstream(
    provider: str,
    geojson_geoms: list[dict],
    version: str | None,
    meta: dict,
    *,
    plausible_km2: tuple[float, float] | None = None,
    valid_at: dt.datetime | None = None,
) -> UpstreamGeometry:
    """Union a provider's polygons into one MultiPolygon and store it as builder input.

    ``ST_MakeValid`` + ``ST_CollectionExtract`` strip the self-intersections and stray
    lines/points that both upstreams occasionally publish.
    """
    import orjson

    features = [orjson.dumps(g).decode() for g in geojson_geoms if g]
    if not features:
        raise ValueError(f"{provider}: no polygon geometries to store")

    row = db.session.execute(
        text(
            """
            WITH parts AS (
                SELECT ST_CollectionExtract(
                           ST_MakeValid(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(g), 4326))), 3
                       ) AS geom
                FROM unnest(CAST(:geoms AS text[])) AS g
            )
            SELECT ST_AsBinary(
                     ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(geom)), 3))
                   ) AS wkb,
                   ST_Area(ST_Union(geom)::geography) / 1e6 AS km2
            FROM parts
            WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
            """
        ),
        {"geoms": features},
    ).first()
    if row is None or row.wkb is None:
        raise ValueError(f"{provider}: union produced no geometry")

    area_km2 = float(row.km2 or 0)
    if plausible_km2 is not None:
        low, high = plausible_km2
        if not low <= area_km2 <= high:
            raise ValueError(
                f"{provider}: unioned area {area_km2:.0f} km² is outside the plausible band "
                f"{low:.0f}–{high:.0f} km² — refusing to store"
            )
    meta = {**meta, "area_km2": round(area_km2, 1)}

    entry = UpstreamGeometry(
        provider=provider,
        upstream_version=str(version) if version is not None else None,
        geom=func_from_wkb(row.wkb),
        meta=meta,
        valid_at=valid_at or dt.datetime.now(dt.timezone.utc),
    )
    db.session.add(entry)
    db.session.commit()
    return entry


def func_from_wkb(wkb: memoryview | bytes):
    """Wrap raw WKB for assignment to a GeoAlchemy2 column."""
    from geoalchemy2.elements import WKBElement

    return WKBElement(bytes(wkb), srid=4326)


def build(force: bool = False, at: dt.datetime | None = None) -> dict:
    """Build one generation of the ``ru`` and ``grey`` snapshots.

    ``at`` builds history: the upstream layers valid at that instant are used, claims are applied
    as of then, and the snapshots are stamped ``valid_at = at``. With ``at`` unset this is the
    live build and ``valid_at`` is now.
    """
    started = time.monotonic()
    deepstate = latest_upstream("deepstate", at)
    isw = latest_upstream("isw", at)
    declared_grey = latest_upstream("deepstate_grey", at)

    ds_ok, isw_ok = _fresh(deepstate) or bool(deepstate), _fresh(isw) or bool(isw)
    if not (ds_ok or isw_ok):
        log.warning("frontline build skipped: no upstream geometry available")
        return {"status": "no_upstream"}

    baselines = {
        "deepstate_id": deepstate.upstream_version if deepstate else None,
        "deepstate_fetched_at": deepstate.fetched_at.isoformat() if deepstate else None,
        "isw_editdate": isw.upstream_version if isw else None,
        "isw_fetched_at": isw.fetched_at.isoformat() if isw else None,
        "deepstate_area_km2": (deepstate.meta or {}).get("area_km2") if deepstate else None,
        "isw_area_km2": (isw.meta or {}).get("area_km2") if isw else None,
        "declared_grey_id": declared_grey.upstream_version if declared_grey else None,
        "degraded": None,
    }

    if deepstate and isw:
        agreed_sql = "ST_Intersection(ds.geom, isw.geom)"
        disagreed_sql = "ST_SymDifference(ds.geom, isw.geom)"
        source_cte = """
            ds AS (SELECT geom FROM upstream_geometries WHERE id = :ds_id),
            isw AS (SELECT geom FROM upstream_geometries WHERE id = :isw_id)
        """
        params = {"ds_id": deepstate.id, "isw_id": isw.id}
    else:
        # One upstream only: no disagreement layer can be computed; record the degradation.
        only = deepstate or isw
        baselines["degraded"] = f"single_upstream:{only.provider}"
        agreed_sql = "ds.geom"
        disagreed_sql = "ST_GeomFromText('MULTIPOLYGON EMPTY', 4326)"
        source_cte = "ds AS (SELECT geom FROM upstream_geometries WHERE id = :ds_id)"
        params = {"ds_id": only.id}
        isw = None

    # "Has the baseline already absorbed this claim?" depends on the instant the baseline
    # *depicts*, not on when we happened to download it. Using fetched_at made the answer depend
    # on write order — a backfill storing historical rows later moved the cutoff around — and
    # wrongly dropped claims resolved after the latest snapshot was published but before it was
    # fetched.
    baseline_cutoff = max(
        [row.valid_at for row in (deepstate, isw) if row is not None],
        default=dt.datetime.now(dt.timezone.utc),
    )

    applied = _applied_claims(baseline_cutoff, at)
    pending_ids = _grey_claims(at)

    params.update(
        {
            "ru_ids": applied["ru_advance"] or [0],
            "ua_ids": applied["ua_advance"] or [0],
            "pending_ids": pending_ids or [0],
            "grey_buffer_m": settings.grey_buffer_km * 1000.0,
            "declared_grey_id": declared_grey.id if declared_grey else None,
            "grey_min_part_m2": settings.grey_min_part_km2 * 1e6,
            "claim_apply_m": settings.claim_apply_km * 1000.0,
        }
    )

    sql = text(
        f"""
        WITH {source_cte},
        agreed AS (SELECT ST_MakeValid({agreed_sql}) AS geom FROM ds{'' if isw is None else ', isw'}),
        disagreed_raw AS (SELECT ST_MakeValid({disagreed_sql}) AS geom FROM ds{'' if isw is None else ', isw'}),
        -- Drop sliver parts of the *derived* disagreement band only; DeepState's declared grey
        -- polygons and pending-claim buffers are deliberate and are never size-filtered.
        disagreed AS (
            SELECT COALESCE(ST_Union(part), ST_GeomFromText('MULTIPOLYGON EMPTY', 4326)) AS geom
            FROM (SELECT (ST_Dump(disagreed_raw.geom)).geom AS part FROM disagreed_raw) parts
            WHERE :grey_min_part_m2 <= 0
               OR ST_Area(part::geography) >= :grey_min_part_m2
        ),
        claim_geom AS (
            SELECT c.id, c.direction,
                   -- gazetteer.boundary is the real settlement outline when we have one; with
                   -- GeoNames as the gazetteer source we never do, so this is always the circle.
                   COALESCE(
                     g.boundary,
                     ST_Multi(ST_Buffer(c.geom::geography, :claim_apply_m)::geometry)
                   ) AS geom
            FROM frontline_claims c
            LEFT JOIN gazetteer g ON g.id = c.gazetteer_id
            WHERE c.id = ANY(:ru_ids) OR c.id = ANY(:ua_ids)
        ),
        ru_add AS (SELECT ST_Union(geom) AS geom FROM claim_geom WHERE id = ANY(:ru_ids)),
        ua_cut AS (SELECT ST_Union(geom) AS geom FROM claim_geom WHERE id = ANY(:ua_ids)),
        pending_buf AS (
            SELECT ST_Union(ST_Buffer(c.geom::geography, :grey_buffer_m)::geometry) AS geom
            FROM frontline_claims c WHERE c.id = ANY(:pending_ids)
        ),
        -- DeepState's own "unknown status" polygons: unclear control stated at the source,
        -- not inferred from a disagreement between datasets.
        declared_grey AS (
            SELECT COALESCE(
                     (SELECT geom FROM upstream_geometries WHERE id = :declared_grey_id),
                     ST_GeomFromText('MULTIPOLYGON EMPTY', 4326)
                   ) AS geom
        ),
        ru_raw AS (
            SELECT ST_MakeValid(
                     ST_Difference(
                       ST_Union(agreed.geom, COALESCE(ru_add.geom, ST_GeomFromText('MULTIPOLYGON EMPTY', 4326))),
                       COALESCE(ua_cut.geom, ST_GeomFromText('MULTIPOLYGON EMPTY', 4326))
                     )
                   ) AS geom
            FROM agreed, ru_add, ua_cut
        ),
        grey_raw AS (
            SELECT ST_MakeValid(
                     ST_Difference(
                       ST_Union(
                         ST_Union(
                           disagreed.geom,
                           COALESCE(pending_buf.geom, ST_GeomFromText('MULTIPOLYGON EMPTY', 4326))
                         ),
                         declared_grey.geom
                       ),
                       COALESCE(ru_add.geom, ST_GeomFromText('MULTIPOLYGON EMPTY', 4326))
                     )
                   ) AS geom
            FROM disagreed, pending_buf, ru_add, declared_grey
        )
        SELECT
            ST_AsBinary(ST_Multi(ST_CollectionExtract(ru_raw.geom, 3)))   AS ru_wkb,
            ST_AsBinary(ST_Multi(ST_CollectionExtract(grey_raw.geom, 3))) AS grey_wkb
        FROM ru_raw, grey_raw
        """
    )
    row = db.session.execute(sql, params).first()
    if row is None or row.ru_wkb is None:
        log.error("frontline build produced no geometry")
        return {"status": "empty"}

    generation_meta = {
        **baselines,
        "applied_claim_ids": applied["ru_advance"] + applied["ua_advance"],
        "pending_claim_ids": [i for i in pending_ids if i],
        "grey_buffer_km": settings.grey_buffer_km,
        "grey_min_part_km2": settings.grey_min_part_km2,
        "grey_claim_ttl_days": settings.grey_claim_ttl_days,
        "claim_apply_km": settings.claim_apply_km,
    }

    valid_at = at or dt.datetime.now(dt.timezone.utc)
    built = []
    for layer, wkb in (("ru", row.ru_wkb), ("grey", row.grey_wkb)):
        if wkb is None:
            continue
        # (layer, valid_at) is unique: re-running a backfill replaces that generation rather than
        # stacking duplicate history.
        db.session.execute(
            text("DELETE FROM frontline_snapshots WHERE layer = :layer AND valid_at = :valid_at"),
            {"layer": layer, "valid_at": valid_at},
        )
        snapshot = FrontlineSnapshot(
            layer=layer,
            geom=func_from_wkb(wkb),
            simplified=_simplified(wkb),
            generation_meta=generation_meta,
            valid_at=valid_at,
        )
        db.session.add(snapshot)
        built.append(layer)
    db.session.commit()

    invalidate("api:frontline")
    if at is None:
        clear_frontline_dirty()
    duration = time.monotonic() - started
    log.info("frontline rebuilt layers=%s in %.1fs", built, duration)
    return {
        "status": "ok",
        "layers": built,
        "valid_at": valid_at.isoformat(),
        "duration_s": round(duration, 2),
        "meta": generation_meta,
    }


def _simplified(wkb: bytes) -> dict:
    """GeoJSON at the three zoom tiers (PLAN §14.4)."""
    import orjson

    out = {}
    for tier, tolerance in settings.snapshot_simplify_tolerances.items():
        row = db.session.execute(
            text(
                "SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology("
                "  ST_GeomFromWKB(:wkb, 4326), :tol), 5) AS gj"
            ),
            {"wkb": bytes(wkb), "tol": float(tolerance)},
        ).first()
        out[tier] = orjson.loads(row.gj) if row and row.gj else EMPTY_MULTIPOLYGON
    return out


def _grey_claims(at: dt.datetime | None = None) -> list[int]:
    """Pending claims still entitled to a grey halo, as of ``at`` (default: now).

    A claim paints grey only while its sighting keeps being repeated. The recency test uses the
    evidence's ``occurred_at`` — when the sighting was reported — not when we ingested it, so a
    backfilled report ages from its own date and a historical rebuild asks "was this still fresh
    *then*". A claim whose evidence has all been deactivated by a debunk has no recent evidence
    left and falls back to its own creation time, so it expires on the same clock.
    """
    as_of = at or dt.datetime.now(dt.timezone.utc)
    rows = db.session.execute(
        text(
            """
            SELECT c.id
            FROM frontline_claims c
            WHERE c.status = 'pending'
              AND c.created_at <= :as_of
              AND COALESCE(
                    (SELECT max(e.occurred_at)
                       FROM evidence_links l
                       JOIN extracted_events e ON e.id = l.event_id
                      WHERE l.claim_id = c.id AND l.active AND e.occurred_at <= :as_of),
                    c.created_at
                  ) >= :as_of - make_interval(secs => :ttl_seconds)
            """
        ),
        {"as_of": as_of, "ttl_seconds": settings.grey_claim_ttl_days * 86400.0},
    ).all()
    return [r.id for r in rows]


def _applied_claims(
    baseline_cutoff: dt.datetime, at: dt.datetime | None = None
) -> dict[str, list[int]]:
    """Confirmed claims newer than both baselines — the fast path ahead of the upstreams.

    Older confirmed claims are dropped: the baselines have already absorbed them, and re-applying
    would double-count. Reverted and rejected claims are never applied, which is the revert.
    """
    query = select(FrontlineClaim.id, FrontlineClaim.direction).where(
        FrontlineClaim.status == "confirmed",
        FrontlineClaim.resolved_at.isnot(None),
        FrontlineClaim.resolved_at > baseline_cutoff,
    )
    if at is not None:
        query = query.where(FrontlineClaim.resolved_at <= at)
    rows = db.session.execute(query).all()
    out: dict[str, list[int]] = {"ru_advance": [], "ua_advance": []}
    for row in rows:
        out[row.direction].append(row.id)
    return out


def latest_snapshots(detail: str = "mid", at: dt.datetime | None = None) -> dict:
    """Payload for ``/api/frontline``.

    ``at`` returns the newest snapshot valid at or before that instant — how the map looked then.
    """
    tolerances = settings.snapshot_simplify_tolerances
    tier = detail if detail in tolerances else "mid"
    layers: dict[str, dict] = {}
    built_at = None
    valid_at = None
    meta: dict = {}
    for layer in ("ru", "grey"):
        query = select(FrontlineSnapshot).where(FrontlineSnapshot.layer == layer)
        if at is not None:
            query = query.where(FrontlineSnapshot.valid_at <= at)
        snapshot = db.session.execute(
            query.order_by(FrontlineSnapshot.valid_at.desc()).limit(1)
        ).scalar_one_or_none()
        if snapshot is None:
            layers[layer] = EMPTY_MULTIPOLYGON
            continue
        layers[layer] = (snapshot.simplified or {}).get(tier) or EMPTY_MULTIPOLYGON
        if valid_at is None or snapshot.valid_at > valid_at:
            valid_at = snapshot.valid_at
            built_at = snapshot.built_at
            meta = snapshot.generation_meta or {}
    return {
        "built_at": built_at.isoformat() if built_at else None,
        "valid_at": valid_at.isoformat() if valid_at else None,
        "requested_at": at.isoformat() if at else None,
        "detail": tier,
        "layers": layers,
        "meta": meta,
    }
