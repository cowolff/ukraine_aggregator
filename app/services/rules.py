"""Claim state machine: intake, corroboration, geo-proof, debunk and revert (PLAN §13).

State machine:  pending → confirmed ⇄ reverted,  pending → rejected.

Two deliberate readings of the spec, documented because they are load-bearing:

* "neutral counts as western" for the ≥2-perspective test, so a neutral+western pair does *not*
  confirm — it is one class of view, not two.
* ``reliability_tier = 3`` sources are excluded from *both* confirmation paths, not only from
  corroboration. A tier-3 outlet posting its own "geolocation" must never move the line alone
  (PLAN §20e), and rule 3.2's trust rests on the proof being credible.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select, text

from app.config import settings
from app.extensions import db, log
from app.models import (
    EvidenceLink,
    ExtractedEvent,
    FrontlineClaim,
    FrontlineSnapshot,
    GazetteerEntry,
    NewsItem,
    Source,
)
from app.services.audit import audit
from app.services.cache import mark_frontline_dirty

# A geolocation proof may confirm a frontline change on its own (PLAN §13, rule 3.2) — but only
# if it actually asserts a side. GeoConfirmed's archive is bulk-ingested *verification* of things:
# a destroyed vehicle, a documented air-defence site, a satellite image of a factory. Those records
# say where something is, not who controls the ground, and they arrive with no `claimed_by`.
# Treating them as control proofs confirmed 961 claims off 7,578 such links and stamped ~12,400 km²
# of asserted control onto the map. A proof with no assessed side is kept as an icon and may
# support an existing claim, but cannot open or confirm one.
def asserts_a_side(claimed_by: str | None) -> bool:
    return claimed_by in ("ru", "ua")


CLAIMABLE_TYPES = ("frontline_advance", "frontline_claim")


def item_location_counts(news_item_ids: list[int]) -> dict[int, int]:
    """How many settlements each item named — the digest test (see settings.claim_max_locations)."""
    if not news_item_ids:
        return {}
    rows = db.session.execute(
        text(
            """
            SELECT news_item_id, count(*) AS n
            FROM extracted_events
            WHERE news_item_id = ANY(:ids)
              AND event_type = ANY(:types)
              AND geom IS NOT NULL
            GROUP BY news_item_id
            """
        ),
        {"ids": news_item_ids, "types": list(CLAIMABLE_TYPES)},
    ).all()
    return {r.news_item_id: int(r.n) for r in rows}


def is_digest(location_count: int | None) -> bool:
    return (location_count or 0) > settings.claim_max_locations


PERSPECTIVE_CLASS = {
    "ukrainian": "ukrainian",
    "russian": "russian",
    "western": "western",
    "neutral": "western",  # PLAN §13: neutral is counted as the western view class
}


@dataclass
class IntakeResult:
    events_seen: int = 0
    claims_opened: int = 0
    links_created: int = 0
    deep_strikes: int = 0
    debunks_matched: int = 0
    digests_skipped: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class EvaluationResult:
    confirmed: int = 0
    reverted: int = 0
    rejected: int = 0
    reconfirmed: int = 0
    reattributed: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


# --------------------------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------------------------
def current_ru_geom():
    """Latest RU-control snapshot geometry, or None before the first build."""
    return db.session.execute(
        select(FrontlineSnapshot.geom)
        .where(FrontlineSnapshot.layer == "ru")
        .order_by(FrontlineSnapshot.built_at.desc())
        .limit(1)
    ).scalar()


def dist_to_line_km(event_id: int) -> float | None:
    """Distance from an event to the frontline (boundary of RU control), in km.

    Returns None when no snapshot exists yet — callers then cannot apply the deep-strike rule and
    must fall back to treating the event as near the line.
    """
    row = db.session.execute(
        text(
            """
            SELECT ST_Distance(e.geom::geography, ST_Boundary(s.geom)::geography) / 1000.0 AS km
            FROM extracted_events e
            CROSS JOIN (
                SELECT geom FROM frontline_snapshots
                WHERE layer = 'ru' ORDER BY built_at DESC LIMIT 1
            ) s
            WHERE e.id = :eid AND e.geom IS NOT NULL
            """
        ),
        {"eid": event_id},
    ).first()
    return float(row.km) if row and row.km is not None else None


def inside_ru_control(event_id: int) -> bool | None:
    row = db.session.execute(
        text(
            """
            SELECT ST_Intersects(e.geom, s.geom) AS inside
            FROM extracted_events e
            CROSS JOIN (
                SELECT geom FROM frontline_snapshots
                WHERE layer = 'ru' ORDER BY built_at DESC LIMIT 1
            ) s
            WHERE e.id = :eid AND e.geom IS NOT NULL
            """
        ),
        {"eid": event_id},
    ).first()
    return bool(row.inside) if row else None


# Statuses a new event may attach to. `reverted` and `confirmed` are included so fresh evidence
# accumulates on the existing claim instead of opening a duplicate — that is what makes the
# reverted → confirmed edge of the state machine reachable (PLAN §13). `rejected` is terminal.
JOINABLE_STATUSES = ("pending", "confirmed", "reverted")


def nearby_claim(event: ExtractedEvent, direction: str | None, statuses=JOINABLE_STATUSES):
    """Live claim within CLAIM_JOIN_KM of the event with a matching direction."""
    params = {
        "eid": event.id,
        "radius": settings.claim_join_km * 1000.0,
        "statuses": list(statuses),
    }
    direction_clause = ""
    if direction:
        direction_clause = "AND c.direction = :direction"
        params["direction"] = direction
    row = db.session.execute(
        text(
            f"""
            SELECT c.id
            FROM frontline_claims c, extracted_events e
            WHERE e.id = :eid
              AND c.status = ANY(:statuses)
              {direction_clause}
              AND ST_DWithin(c.geom::geography, e.geom::geography, :radius)
            ORDER BY array_position(CAST(:statuses AS text[]), c.status),
                     ST_Distance(c.geom::geography, e.geom::geography)
            LIMIT 1
            """
        ),
        params,
    ).first()
    return db.session.get(FrontlineClaim, row.id) if row else None


# --------------------------------------------------------------------------------------------
# intake
# --------------------------------------------------------------------------------------------
def _link(claim: FrontlineClaim, event: ExtractedEvent, role: str) -> EvidenceLink | None:
    existing = db.session.execute(
        select(EvidenceLink).where(
            EvidenceLink.claim_id == claim.id, EvidenceLink.event_id == event.id
        )
    ).scalar_one_or_none()
    if existing:
        return None
    link = EvidenceLink(claim_id=claim.id, event_id=event.id, role=role, active=True)
    db.session.add(link)
    claim.updated_at = dt.datetime.now(dt.timezone.utc)
    return link


def _open_claim(event: ExtractedEvent, direction: str) -> FrontlineClaim:
    claim = FrontlineClaim(
        geom=event.geom,
        gazetteer_id=event.gazetteer_id,
        direction=direction,
        status="pending",
    )
    db.session.add(claim)
    db.session.flush()
    return claim


def _is_usable_evidence(event: ExtractedEvent) -> bool:
    """PLAN §21: low-confidence extractions render faintly but never count as evidence."""
    if event.confidence is None:
        return True
    return float(event.confidence) >= settings.low_confidence_floor


def intake_events(limit: int = 500) -> IntakeResult:
    result = IntakeResult()
    events = (
        db.session.execute(
            select(ExtractedEvent)
            .where(
                ExtractedEvent.linked.is_(False),
                or_(ExtractedEvent.geom.isnot(None), ExtractedEvent.event_type == "debunk"),
            )
            .order_by(ExtractedEvent.id)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    counts = item_location_counts([e.news_item_id for e in events])
    for event in events:
        result.events_seen += 1
        event.linked = True  # processed exactly once, whatever the outcome
        try:
            _intake_one(event, result, counts.get(event.news_item_id))
        except Exception as exc:  # one bad event must not stall the queue
            log.warning("intake failed for event %s: %s", event.id, exc)
    db.session.commit()
    if result.claims_opened or result.links_created or result.debunks_matched:
        mark_frontline_dirty()
    return result


def _intake_one(
    event: ExtractedEvent, result: IntakeResult, location_count: int | None = None
) -> None:
    if event.event_type == "debunk":
        if _handle_debunk(event):
            result.debunks_matched += 1
        return

    if event.geom is None:
        return

    distance = dist_to_line_km(event.id)
    far_behind = distance is not None and distance > settings.deep_strike_km
    if event.event_type == "deep_strike" or far_behind:
        # Rule 3.3: icon only, never touches claims or the grey zone.
        result.deep_strikes += 1
        return

    if not _is_usable_evidence(event):
        return

    if event.event_type == "geolocation_proof":
        claim = nearby_claim(event, None)
        if claim is None:
            if not asserts_a_side(event.claimed_by):
                # Verification of a thing, not a claim about control: nothing to open.
                return
            direction = _direction_for_proof(event)
            if direction is None:
                return
            claim = _open_claim(event, direction)
            result.claims_opened += 1
        if _link(claim, event, "geolocation_proof"):
            result.links_created += 1
        return

    if event.event_type in CLAIMABLE_TYPES and event.claimed_by:
        if is_digest(location_count):
            # A roundup naming many settlements: keep the icon, claim nothing.
            result.digests_skipped += 1
            return
        direction = "ru_advance" if event.claimed_by == "ru" else "ua_advance"
        claim = nearby_claim(event, direction)
        if claim is None:
            claim = _open_claim(event, direction)
            result.claims_opened += 1
        if _link(claim, event, "support"):
            result.links_created += 1


def _direction_for_proof(event: ExtractedEvent) -> str | None:
    """A proof with no nearby claim opens one; direction from claimed_by, else from geometry."""
    if event.claimed_by:
        return "ru_advance" if event.claimed_by == "ru" else "ua_advance"
    inside = inside_ru_control(event.id)
    if inside is None:
        return None
    return "ru_advance" if inside else "ua_advance"


def _handle_debunk(event: ExtractedEvent) -> bool:
    """Retract prior geolocation proofs a debunk item disputes (PLAN §13, rule 3.2)."""
    target = (event.llm_raw or {}).get("debunk_target") if event.llm_raw else None
    proofs = _debunk_targets(event, target)
    if not proofs:
        return False
    touched: set[int] = set()
    for link in proofs:
        if not link.active:
            continue
        link.active = False
        touched.add(link.claim_id)
        audit(
            "rule:debunk",
            "evidence.deactivate",
            entity="evidence_links",
            entity_id=str(link.id),
            detail={"claim_id": link.claim_id, "debunk_event_id": event.id},
        )
        # The debunk itself is recorded as evidence on the same claim for the audit trail.
        claim = db.session.get(FrontlineClaim, link.claim_id)
        if claim:
            _link(claim, event, "debunk")
    db.session.flush()
    for claim_id in touched:
        claim = db.session.get(FrontlineClaim, claim_id)
        if claim:
            _reevaluate_confirmed(claim, EvaluationResult())
    return bool(touched)


def _debunk_targets(event: ExtractedEvent, target: str | None) -> list[EvidenceLink]:
    """(a) URL mentioned in debunk_target matches a proof's news URL, else
    (b) same settlement (or within 10 km) with the proof at most 30 days older."""
    proof_links = select(EvidenceLink).where(
        EvidenceLink.role == "geolocation_proof", EvidenceLink.active.is_(True)
    )

    if target and ("http://" in target or "https://" in target):
        url = target.strip().rstrip("/")
        rows = (
            db.session.execute(
                proof_links.join(ExtractedEvent, EvidenceLink.event_id == ExtractedEvent.id)
                .join(NewsItem, ExtractedEvent.news_item_id == NewsItem.id)
                .where(NewsItem.url.isnot(None), func.rtrim(NewsItem.url, "/") == url)
            )
            .scalars()
            .all()
        )
        if rows:
            return list(rows)

    horizon = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)

    if event.gazetteer_id:
        rows = (
            db.session.execute(
                proof_links.join(ExtractedEvent, EvidenceLink.event_id == ExtractedEvent.id).where(
                    ExtractedEvent.gazetteer_id == event.gazetteer_id,
                    ExtractedEvent.created_at >= horizon,
                )
            )
            .scalars()
            .all()
        )
        if rows:
            return list(rows)

    if event.geom is not None:
        ids = [
            r.id
            for r in db.session.execute(
                text(
                    """
                    SELECT l.id
                    FROM evidence_links l
                    JOIN extracted_events p ON p.id = l.event_id
                    JOIN extracted_events d ON d.id = :eid
                    WHERE l.role = 'geolocation_proof' AND l.active
                      AND p.created_at >= :horizon
                      AND p.geom IS NOT NULL
                      AND ST_DWithin(p.geom::geography, d.geom::geography, 10000)
                    """
                ),
                {"eid": event.id, "horizon": horizon},
            ).all()
        ]
        if ids:
            return [db.session.get(EvidenceLink, i) for i in ids]

    # Name-only debunk: fall back to matching the disputed settlement by name.
    if target:
        from app.services.geocode import match_place

        match = match_place(target)
        if match and not match.ambiguous:
            rows = (
                db.session.execute(
                    proof_links.join(
                        ExtractedEvent, EvidenceLink.event_id == ExtractedEvent.id
                    ).where(
                        ExtractedEvent.gazetteer_id == match.gazetteer_id,
                        ExtractedEvent.created_at >= horizon,
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)
    return []


# --------------------------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------------------------
def _evidence_rows(claim: FrontlineClaim) -> list[dict]:
    """Active support/proof links resolved to source perspective + tier, evidence-usable only."""
    rows = db.session.execute(
        text(
            """
            SELECT l.id, l.role, l.active, e.confidence, e.claimed_by, e.event_type,
                   (SELECT count(*) FROM extracted_events x
                     WHERE x.news_item_id = e.news_item_id
                       AND x.event_type IN ('frontline_advance','frontline_claim')
                       AND x.geom IS NOT NULL) AS item_locations,
                   s.perspective, s.reliability_tier,
                   s.name AS source_name, n.url, n.title, n.published_at, e.id AS event_id
            FROM evidence_links l
            JOIN extracted_events e ON e.id = l.event_id
            JOIN news_items n ON n.id = e.news_item_id
            JOIN sources s ON s.id = n.source_id
            WHERE l.claim_id = :cid
            ORDER BY l.id
            """
        ),
        {"cid": claim.id},
    ).mappings().all()
    return [dict(r) for r in rows]


def _confirmation(rows: list[dict]) -> tuple[bool, str | None, dict]:
    """Return (should_confirm, resolved_by, detail) over *active* evidence only."""
    usable = [
        r
        for r in rows
        if r["active"]
        and r["role"] in ("support", "geolocation_proof")
        and int(r["reliability_tier"]) < 3
        and (r["confidence"] is None or float(r["confidence"]) >= settings.low_confidence_floor)
        # A geolocation that names no side asserts nothing about control, so it counts toward
        # neither confirmation path — not rule 3.2 on its own, and not as an independent
        # perspective under rule 3.1. It stays linked for the audit trail and on the map.
        and (r["role"] != "geolocation_proof" or asserts_a_side(r["claimed_by"]))
        # Nor does a daily roundup that happens to list this settlement among dozens.
        and not (r["event_type"] in CLAIMABLE_TYPES and is_digest(r["item_locations"]))
    ]
    perspectives = {PERSPECTIVE_CLASS.get(r["perspective"], "western") for r in usable}
    # Only a proof that names a side can carry rule 3.2 on its own.
    proofs = [r for r in usable if r["role"] == "geolocation_proof"]
    detail = {
        "perspectives": sorted(perspectives),
        "proof_count": len(proofs),
        "unassessed_proofs": sum(
            1 for r in rows
            if r["active"] and r["role"] == "geolocation_proof"
            and not asserts_a_side(r["claimed_by"])
        ),
        "usable_links": len(usable),
    }
    if proofs:
        return True, "rule:geoproof", detail
    if len(perspectives) >= 2:
        return True, "rule:corroboration", detail
    return False, None, detail


def _set_status(claim: FrontlineClaim, status: str, resolved_by: str, detail: dict) -> None:
    claim.status = status
    claim.resolved_by = resolved_by
    claim.resolved_at = dt.datetime.now(dt.timezone.utc)
    claim.updated_at = claim.resolved_at
    audit(
        resolved_by,
        f"claim.{status}",
        entity="frontline_claims",
        entity_id=str(claim.id),
        detail=detail,
    )
    mark_frontline_dirty()


def _revert_reason(rows: list[dict]) -> str:
    """Why a confirmed claim stopped qualifying.

    Attributing every revert to a debunk is wrong and misleads the audit trail: a claim can also
    lose its footing because evidence was deactivated by an admin, or because the bar itself moved.
    Only call it a debunk when there is actually a debunk link.
    """
    if any(r["role"] == "debunk" for r in rows):
        return "rule:debunk"
    if any(r["role"] == "geolocation_proof" and not r["active"] for r in rows):
        return "rule:evidence_retracted"
    return "rule:insufficient_evidence"


def _reevaluate_confirmed(claim: FrontlineClaim, result: EvaluationResult) -> None:
    if claim.status != "confirmed":
        return
    rows = _evidence_rows(claim)
    ok, _resolved_by, detail = _confirmation(rows)
    if not ok:
        _set_status(claim, "reverted", _revert_reason(rows), detail)
        result.reverted += 1


def evaluate_claims() -> EvaluationResult:
    result = EvaluationResult()
    now = dt.datetime.now(dt.timezone.utc)
    stale_before = now - dt.timedelta(days=settings.claim_stale_days)

    claims = (
        db.session.execute(
            select(FrontlineClaim)
            .where(FrontlineClaim.status.in_(("pending", "confirmed", "reverted")))
            .order_by(FrontlineClaim.id)
        )
        .scalars()
        .all()
    )

    for claim in claims:
        rows = _evidence_rows(claim)
        ok, resolved_by, detail = _confirmation(rows)

        if claim.status == "pending":
            if ok:
                _set_status(claim, "confirmed", resolved_by, detail)
                result.confirmed += 1
            elif claim.created_at and claim.created_at < stale_before:
                last_evidence = max(
                    (r["published_at"] for r in rows if r["published_at"]), default=None
                )
                if last_evidence is None or last_evidence < stale_before:
                    _set_status(claim, "rejected", "rule:stale", detail)
                    result.rejected += 1
        elif claim.status == "confirmed":
            if not ok:
                _set_status(claim, "reverted", _revert_reason(rows), detail)
                result.reverted += 1
            elif (
                resolved_by
                and claim.resolved_by != resolved_by
                and (claim.resolved_by or "").startswith("rule:")
            ):
                # Still confirmed, but for a different reason than recorded — e.g. the geo-proof
                # that originally carried it no longer counts and corroboration now does. An
                # admin's own attribution is never overwritten.
                claim.resolved_by = resolved_by
                claim.updated_at = dt.datetime.now(dt.timezone.utc)
                audit(
                    resolved_by, "claim.reattributed", entity="frontline_claims",
                    entity_id=str(claim.id),
                    detail={"was": claim.resolved_by, "now": resolved_by, **detail},
                )
                result.reattributed += 1
        elif claim.status == "reverted":
            # Reverted claims can return to confirmed when new active evidence arrives.
            if ok:
                _set_status(claim, "confirmed", resolved_by, detail)
                result.reconfirmed += 1

    db.session.commit()
    return result


def evidence_chain(claim_id: int) -> list[dict]:
    """Human-readable evidence chain for the admin claims-review page (PLAN §17.4)."""
    claim = db.session.get(FrontlineClaim, claim_id)
    if not claim:
        return []
    out = []
    for row in _evidence_rows(claim):
        out.append(
            {
                "link_id": row["id"],
                "event_id": row["event_id"],
                "role": row["role"],
                "active": row["active"],
                "source_name": row["source_name"],
                "perspective": row["perspective"],
                "reliability_tier": row["reliability_tier"],
                "confidence": row["confidence"],
                "title": row["title"],
                "url": row["url"],
                "published_at": row["published_at"].isoformat() if row["published_at"] else None,
            }
        )
    return out


def claim_settlement_geom_sql() -> str:
    """Geometry a confirmed claim contributes: settlement boundary if known, else 2 km buffer."""
    return """
        COALESCE(
            g.boundary,
            ST_Multi(ST_Buffer(c.geom::geography, 2000)::geometry)
        )
    """


def unresolved_claim_ids() -> list[int]:
    return [
        r.id
        for r in db.session.execute(
            select(FrontlineClaim.id).where(FrontlineClaim.status == "pending")
        ).all()
    ]
