"""Admin pages 1-6 of PLAN §17. Every mutation writes an audit_log row."""
from __future__ import annotations

import datetime as dt

import orjson
from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, text

from app.admin import bp
from app.admin.forms import BlackoutForm, EventLocationForm, NotificationForm, SourceForm
from app.extensions import db
from app.models import (
    EVENT_TYPES,
    BlackoutZone,
    EvidenceLink,
    ExtractedEvent,
    FrontlineClaim,
    NewsItem,
    Notification,
    Source,
    SynthesizedReport,
)
from app.services.audit import audit, current_actor
from app.services.cache import invalidate, mark_frontline_dirty
from app.services.health import dashboard_stats
from app.services.rules import evidence_chain


@bp.before_request
def _require_login():
    if request.endpoint in ("admin.login", "admin.static"):
        return None
    if not current_user.is_authenticated:
        return redirect(url_for("admin.login", next=request.path))
    return None


def _invalidate_public() -> None:
    invalidate("api:events")
    invalidate("api:news")
    invalidate("api:notifications")


# --------------------------------------------------------------------------------------------
# 1. dashboard
# --------------------------------------------------------------------------------------------
@bp.get("/")
@login_required
def dashboard():
    stats = dashboard_stats()
    sources = (
        db.session.execute(
            select(Source).where(Source.enabled).order_by(Source.status.desc(), Source.name)
        )
        .scalars()
        .all()
    )
    recent_audit = db.session.execute(
        text("SELECT at, actor, action, entity, entity_id FROM audit_log ORDER BY at DESC LIMIT 25")
    ).mappings().all()
    return render_template(
        "dashboard.html", stats=stats, sources=sources, audit_entries=recent_audit
    )


# --------------------------------------------------------------------------------------------
# 2. sources
# --------------------------------------------------------------------------------------------
@bp.get("/sources")
@login_required
def sources_list():
    query = select(Source)
    q = (request.args.get("q") or "").strip()
    if q:
        query = query.where(Source.name.ilike(f"%{q}%") | Source.url.ilike(f"%{q}%"))
    for field in ("type", "perspective", "status"):
        value = request.args.get(field)
        if value:
            query = query.where(getattr(Source, field) == value)
    if request.args.get("enabled") == "1":
        query = query.where(Source.enabled)
    elif request.args.get("enabled") == "0":
        query = query.where(~Source.enabled)
    rows = db.session.execute(query.order_by(Source.name).limit(500)).scalars().all()
    return render_template("sources.html", sources=rows, q=q, args=request.args)


@bp.route("/sources/new", methods=["GET", "POST"])
@bp.route("/sources/<int:source_id>", methods=["GET", "POST"])
@login_required
def source_edit(source_id: int | None = None):
    source = db.session.get(Source, source_id) if source_id else None
    if source_id and source is None:
        abort(404)
    form = SourceForm(obj=source)
    if source and request.method == "GET":
        form.meta_json.data = orjson.dumps(source.meta or {}).decode()
    if form.validate_on_submit():
        try:
            meta = orjson.loads(form.meta_json.data) if (form.meta_json.data or "").strip() else {}
            if not isinstance(meta, dict):
                raise ValueError("meta must be a JSON object")
        except (orjson.JSONDecodeError, ValueError) as exc:
            flash(f"Meta JSON invalid: {exc}", "error")
            return render_template("source_form.html", form=form, source=source)
        if source is None:
            source = Source(name=form.name.data, type=form.type.data, url=form.url.data,
                            perspective=form.perspective.data)
            db.session.add(source)
        source.name = form.name.data
        source.type = form.type.data
        source.url = form.url.data
        source.perspective = form.perspective.data
        source.reliability_tier = form.reliability_tier.data
        source.poll_interval_s = form.poll_interval_s.data
        source.enabled = bool(form.enabled.data)
        source.meta = meta
        db.session.flush()
        audit(current_actor(), "source.save", entity="sources", entity_id=str(source.id),
              detail={"name": source.name, "enabled": source.enabled})
        db.session.commit()
        flash(f"Saved source “{source.name}”.", "ok")
        return redirect(url_for("admin.sources_list"))
    return render_template("source_form.html", form=form, source=source)


@bp.post("/sources/<int:source_id>/delete")
@login_required
def source_delete(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    name = source.name
    linked = db.session.execute(
        text("SELECT count(*) FROM news_items WHERE source_id = :sid"), {"sid": source_id}
    ).scalar()
    if linked:
        # Keep referential history: disable instead of orphaning ingested news.
        source.enabled = False
        source.status = "dead"
        flash(f"“{name}” has {linked} stored items — disabled instead of deleted.", "warn")
    else:
        db.session.delete(source)
        flash(f"Deleted source “{name}”.", "ok")
    audit(current_actor(), "source.delete", entity="sources", entity_id=str(source_id),
          detail={"name": name, "kept_history": bool(linked)})
    db.session.commit()
    return redirect(url_for("admin.sources_list"))


@bp.post("/sources/<int:source_id>/reset")
@login_required
def source_reset(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    source.consecutive_failures = 0
    source.status = "ok"
    audit(current_actor(), "source.reset_failures", entity="sources", entity_id=str(source_id))
    db.session.commit()
    flash(f"Reset failure counter for “{source.name}”.", "ok")
    return redirect(request.referrer or url_for("admin.sources_list"))


@bp.post("/sources/<int:source_id>/test")
@login_required
def source_test(source_id: int):
    """Run the adapter once inline and show the first three parsed items (PLAN §17.2)."""
    source = db.session.get(Source, source_id) or abort(404)
    from celery_worker.adapters import fetch_source_preview

    try:
        items, note = fetch_source_preview(source)
        preview = items[:3]
    except Exception as exc:  # adapters must never 500 the admin UI
        preview, note = [], f"{type(exc).__name__}: {exc}"
    audit(current_actor(), "source.test_fetch", entity="sources", entity_id=str(source_id),
          detail={"parsed": len(preview), "note": note})
    db.session.commit()
    return render_template("source_test.html", source=source, preview=preview, note=note)


# --------------------------------------------------------------------------------------------
# 3. news & events
# --------------------------------------------------------------------------------------------
@bp.get("/news")
@login_required
def news_list():
    clauses, params = ["1=1"], {}
    q = (request.args.get("q") or "").strip()
    if q:
        clauses.append("(n.title ILIKE :q OR n.body ILIKE :q)")
        params["q"] = f"%{q}%"
    status = request.args.get("llm_status")
    if status:
        clauses.append("n.llm_status = :status")
        params["status"] = status
    source_id = request.args.get("source_id")
    if source_id and source_id.isdigit():
        clauses.append("n.source_id = :sid")
        params["sid"] = int(source_id)
    params["limit"] = 100
    rows = db.session.execute(
        text(
            f"""
            SELECT n.id, n.title, n.url, n.published_at, n.fetched_at, n.llm_status, n.visible,
                   s.name AS source_name, s.perspective,
                   (SELECT count(*) FROM extracted_events e WHERE e.news_item_id = n.id) AS event_count,
                   (SELECT count(*) FROM extracted_events e
                     WHERE e.news_item_id = n.id AND e.geom IS NOT NULL) AS placed_count
            FROM news_items n JOIN sources s ON s.id = n.source_id
            WHERE {' AND '.join(clauses)}
            ORDER BY n.id DESC LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return render_template("news.html", rows=rows, args=request.args)


@bp.get("/news/<int:item_id>")
@login_required
def news_detail(item_id: int):
    item = db.session.get(NewsItem, item_id) or abort(404)
    return render_template("news_detail.html", item=item, event_types=EVENT_TYPES,
                           form=EventLocationForm())


@bp.post("/news/<int:item_id>/visible")
@login_required
def news_toggle(item_id: int):
    item = db.session.get(NewsItem, item_id) or abort(404)
    item.visible = not item.visible
    audit(current_actor(), "news.visibility", entity="news_items", entity_id=str(item_id),
          detail={"visible": item.visible})
    db.session.commit()
    _invalidate_public()
    return redirect(request.referrer or url_for("admin.news_list"))


@bp.post("/news/<int:item_id>/delete")
@login_required
def news_delete(item_id: int):
    item = db.session.get(NewsItem, item_id) or abort(404)
    db.session.delete(item)
    audit(current_actor(), "news.delete", entity="news_items", entity_id=str(item_id))
    db.session.commit()
    _invalidate_public()
    flash("News item deleted.", "ok")
    return redirect(url_for("admin.news_list"))


@bp.post("/news/<int:item_id>/reextract")
@login_required
def news_reextract(item_id: int):
    item = db.session.get(NewsItem, item_id) or abort(404)
    for event in list(item.events):
        db.session.delete(event)
    item.llm_status = "pending"
    item.llm_attempts = 0
    audit(current_actor(), "news.reextract", entity="news_items", entity_id=str(item_id))
    db.session.commit()
    from celery_worker.tasks.extract import llm_extract_batch

    try:
        llm_extract_batch.delay(item_ids=[item_id])
        flash("Re-extraction queued.", "ok")
    except Exception as exc:
        flash(f"Marked pending; could not reach the broker ({exc}).", "warn")
    return redirect(url_for("admin.news_detail", item_id=item_id))


@bp.post("/events/<int:event_id>/visible")
@login_required
def event_toggle(event_id: int):
    event = db.session.get(ExtractedEvent, event_id) or abort(404)
    event.visible = not event.visible
    audit(current_actor(), "event.visibility", entity="extracted_events", entity_id=str(event_id),
          detail={"visible": event.visible})
    db.session.commit()
    _invalidate_public()
    return redirect(request.referrer or url_for("admin.news_list"))


@bp.post("/events/<int:event_id>/location")
@login_required
def event_relocate(event_id: int):
    event = db.session.get(ExtractedEvent, event_id) or abort(404)
    form = EventLocationForm()
    if not form.validate_on_submit():
        flash("Latitude and longitude are required.", "error")
        return redirect(url_for("admin.news_detail", item_id=event.news_item_id))
    try:
        lat, lon = float(form.lat.data), float(form.lon.data)
    except ValueError:
        flash("Latitude/longitude must be numbers.", "error")
        return redirect(url_for("admin.news_detail", item_id=event.news_item_id))
    db.session.execute(
        text("UPDATE extracted_events SET geom = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), "
             "coord_source = 'gazetteer_match' WHERE id = :eid"),
        {"lon": lon, "lat": lat, "eid": event_id},
    )
    audit(current_actor(), "event.relocate", entity="extracted_events", entity_id=str(event_id),
          detail={"lat": lat, "lon": lon})
    db.session.commit()
    _invalidate_public()
    mark_frontline_dirty()
    flash("Event moved.", "ok")
    return redirect(url_for("admin.news_detail", item_id=event.news_item_id))


@bp.get("/unplaced")
@login_required
def unplaced_list():
    """Review queue for events the gazetteer matcher refused to place (PLAN §12: unplaced beats
    wrong). Each row offers the matcher's best candidates for one-click assignment."""
    from app.services.geocode import candidates, canonical_oblast, normalize

    rows = db.session.execute(
        text(
            """
            SELECT e.id, e.event_type, e.place_name_raw, e.created_at, e.occurred_at,
                   e.llm_raw->>'oblast' AS hint, n.id AS news_id,
                   coalesce(n.title_en, n.title) AS news_title
            FROM extracted_events e JOIN news_items n ON n.id = e.news_item_id
            WHERE e.geom IS NULL AND e.place_name_raw IS NOT NULL AND e.visible
            ORDER BY e.id DESC LIMIT 50
            """
        )
    ).mappings().all()
    suggestions: dict[int, list[dict]] = {}
    for row in rows:
        query = normalize(row["place_name_raw"])
        if len(query) < 3:
            suggestions[row["id"]] = []
            continue
        hint = canonical_oblast(row["hint"])
        ranked = candidates(query, limit=5, oblast=hint) if hint else []
        seen = {c["id"] for c in ranked}
        ranked += [c for c in candidates(query, limit=5) if c["id"] not in seen]
        suggestions[row["id"]] = ranked[:5]
    return render_template("unplaced.html", rows=rows, suggestions=suggestions)


@bp.post("/events/<int:event_id>/assign/<int:gazetteer_id>")
@login_required
def event_assign(event_id: int, gazetteer_id: int):
    event = db.session.get(ExtractedEvent, event_id) or abort(404)
    updated = db.session.execute(
        text(
            "UPDATE extracted_events SET geom = g.geom, gazetteer_id = g.id, "
            "coord_source = 'gazetteer_match', "
            "geo_meta = jsonb_build_object('resolution', 'manual', 'hint', llm_raw->>'oblast') "
            "FROM gazetteer g WHERE extracted_events.id = :eid AND g.id = :gid"
        ),
        {"eid": event_id, "gid": gazetteer_id},
    )
    if updated.rowcount != 1:
        abort(404)
    audit(current_actor(), "event.assign", entity="extracted_events", entity_id=str(event_id),
          detail={"gazetteer_id": gazetteer_id})
    db.session.commit()
    _invalidate_public()
    mark_frontline_dirty()
    flash(f"Event {event_id} placed.", "ok")
    return redirect(request.referrer or url_for("admin.unplaced_list"))


# --------------------------------------------------------------------------------------------
# 4. claims review
# --------------------------------------------------------------------------------------------
@bp.get("/claims")
@login_required
def claims_list():
    status = request.args.get("status") or "pending"
    query = select(FrontlineClaim).order_by(FrontlineClaim.updated_at.desc()).limit(200)
    if status != "all":
        query = query.where(FrontlineClaim.status == status)
    claims = db.session.execute(query).scalars().all()
    chains = {c.id: evidence_chain(c.id) for c in claims}
    points = db.session.execute(
        text("SELECT id, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM frontline_claims "
             "WHERE id = ANY(:ids)"),
        {"ids": [c.id for c in claims] or [0]},
    ).mappings().all()
    coords = {p["id"]: (p["lat"], p["lon"]) for p in points}
    return render_template("claims.html", claims=claims, chains=chains, coords=coords, status=status)


@bp.post("/claims/<int:claim_id>/<action>")
@login_required
def claim_action(claim_id: int, action: str):
    claim = db.session.get(FrontlineClaim, claim_id) or abort(404)
    mapping = {"confirm": "confirmed", "reject": "rejected", "revert": "reverted",
               "reopen": "pending"}
    if action not in mapping:
        abort(400)
    claim.status = mapping[action]
    claim.resolved_by = current_actor()
    claim.resolved_at = dt.datetime.now(dt.timezone.utc)
    claim.updated_at = claim.resolved_at
    audit(current_actor(), f"claim.{mapping[action]}", entity="frontline_claims",
          entity_id=str(claim_id), detail={"manual": True})
    db.session.commit()
    mark_frontline_dirty()
    from celery_worker.tasks.frontline import rebuild_frontline

    try:
        rebuild_frontline.delay()
    except Exception:
        pass  # the beat schedule will pick up the dirty flag
    flash(f"Claim {claim_id} → {mapping[action]}; frontline rebuild queued.", "ok")
    return redirect(request.referrer or url_for("admin.claims_list"))


@bp.post("/evidence/<int:link_id>/<action>")
@login_required
def evidence_action(link_id: int, action: str):
    link = db.session.get(EvidenceLink, link_id) or abort(404)
    if action not in ("activate", "deactivate"):
        abort(400)
    link.active = action == "activate"
    audit(current_actor(), f"evidence.{action}", entity="evidence_links", entity_id=str(link_id),
          detail={"claim_id": link.claim_id})
    db.session.commit()
    from celery_worker.tasks.rules import evaluate_claims

    mark_frontline_dirty()
    try:
        evaluate_claims.delay()
    except Exception:
        pass
    flash(f"Evidence link {link_id} {action}d; claims re-evaluation queued.", "ok")
    return redirect(request.referrer or url_for("admin.claims_list"))


# --------------------------------------------------------------------------------------------
# 5. notifications
# --------------------------------------------------------------------------------------------
@bp.get("/notifications")
@login_required
def notifications_list():
    rows = db.session.execute(
        select(Notification).order_by(Notification.id.desc())
    ).scalars().all()
    return render_template("notifications.html", rows=rows)


@bp.route("/notifications/new", methods=["GET", "POST"])
@bp.route("/notifications/<int:notification_id>", methods=["GET", "POST"])
@login_required
def notification_edit(notification_id: int | None = None):
    row = db.session.get(Notification, notification_id) if notification_id else None
    if notification_id and row is None:
        abort(404)
    form = NotificationForm(obj=row)
    if form.validate_on_submit():
        if row is None:
            row = Notification(title=form.title.data)
            db.session.add(row)
        row.title = form.title.data
        row.body = form.body.data
        row.level = form.level.data
        row.active = bool(form.active.data)
        row.starts_at = _as_utc(form.starts_at.data)
        row.ends_at = _as_utc(form.ends_at.data)
        db.session.flush()
        audit(current_actor(), "notification.save", entity="notifications", entity_id=str(row.id),
              detail={"title": row.title, "active": row.active})
        db.session.commit()
        invalidate("api:notifications")
        flash("Notification saved.", "ok")
        return redirect(url_for("admin.notifications_list"))
    return render_template("notification_form.html", form=form, row=row)


@bp.post("/notifications/<int:notification_id>/delete")
@login_required
def notification_delete(notification_id: int):
    row = db.session.get(Notification, notification_id) or abort(404)
    db.session.delete(row)
    audit(current_actor(), "notification.delete", entity="notifications",
          entity_id=str(notification_id))
    db.session.commit()
    invalidate("api:notifications")
    flash("Notification deleted.", "ok")
    return redirect(url_for("admin.notifications_list"))


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


# --------------------------------------------------------------------------------------------
# 6. blackout zones
# --------------------------------------------------------------------------------------------
@bp.get("/blackouts")
@login_required
def blackouts_list():
    rows = db.session.execute(
        text(
            "SELECT id, name, reason, active, created_at, created_by, "
            "ST_AsGeoJSON(geom) AS geojson FROM blackout_zones ORDER BY id DESC"
        )
    ).mappings().all()
    return render_template("blackouts.html", rows=rows, form=BlackoutForm())


@bp.post("/blackouts")
@login_required
def blackout_create():
    form = BlackoutForm()
    if not form.validate_on_submit():
        flash("Name and polygon are required.", "error")
        return redirect(url_for("admin.blackouts_list"))
    try:
        geometry = orjson.loads(form.geojson.data)
        if geometry.get("type") == "Feature":
            geometry = geometry["geometry"]
        if geometry.get("type") != "Polygon":
            raise ValueError("geometry must be a Polygon")
    except (orjson.JSONDecodeError, KeyError, ValueError, AttributeError) as exc:
        flash(f"Invalid GeoJSON: {exc}", "error")
        return redirect(url_for("admin.blackouts_list"))

    row = db.session.execute(
        text(
            "INSERT INTO blackout_zones (name, reason, geom, active, created_by) "
            "VALUES (:name, :reason, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326), :active, :actor) "
            "RETURNING id"
        ),
        {
            "name": form.name.data,
            "reason": form.reason.data,
            "geojson": orjson.dumps(geometry).decode(),
            "active": bool(form.active.data),
            "actor": current_actor(),
        },
    ).first()
    audit(current_actor(), "blackout.create", entity="blackout_zones", entity_id=str(row.id),
          detail={"name": form.name.data})
    db.session.commit()
    _invalidate_public()
    flash("Blackout zone created.", "ok")
    return redirect(url_for("admin.blackouts_list"))


@bp.post("/blackouts/<int:zone_id>/toggle")
@login_required
def blackout_toggle(zone_id: int):
    zone = db.session.get(BlackoutZone, zone_id) or abort(404)
    zone.active = not zone.active
    audit(current_actor(), "blackout.toggle", entity="blackout_zones", entity_id=str(zone_id),
          detail={"active": zone.active})
    db.session.commit()
    _invalidate_public()
    return redirect(url_for("admin.blackouts_list"))


@bp.post("/blackouts/<int:zone_id>/delete")
@login_required
def blackout_delete(zone_id: int):
    zone = db.session.get(BlackoutZone, zone_id) or abort(404)
    db.session.delete(zone)
    audit(current_actor(), "blackout.delete", entity="blackout_zones", entity_id=str(zone_id))
    db.session.commit()
    _invalidate_public()
    flash("Blackout zone deleted.", "ok")
    return redirect(url_for("admin.blackouts_list"))


# --------------------------------------------------------------------------------------------
# 7. synthesized reports (plans/SYNTHESIS.md)
# --------------------------------------------------------------------------------------------
@bp.get("/synthesis")
@login_required
def synthesis_list():
    clauses, params = ["1=1"], {}
    verdict = request.args.get("verdict")
    if verdict:
        clauses.append("r.credibility = :verdict")
        params["verdict"] = verdict
    status = request.args.get("llm_status")
    if status:
        clauses.append("r.llm_status = :status")
        params["status"] = status
    params["limit"] = 100
    rows = db.session.execute(
        text(
            f"""
            SELECT r.id, r.event_type, r.place_name, r.status, r.llm_status, r.llm_attempts,
                   r.headline_en, r.credibility, r.cred_meta, r.member_count, r.visible,
                   r.last_reported_at
            FROM synthesized_reports r
            WHERE {' AND '.join(clauses)}
            ORDER BY r.last_reported_at DESC NULLS LAST, r.id DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return render_template("synthesis.html", rows=rows, args=request.args)


@bp.post("/synthesis/<int:report_id>/visible")
@login_required
def synthesis_toggle(report_id: int):
    report = db.session.get(SynthesizedReport, report_id) or abort(404)
    report.visible = not report.visible
    audit(current_actor(), "synthesis.visibility", entity="synthesized_reports",
          entity_id=str(report_id), detail={"visible": report.visible})
    db.session.commit()
    invalidate("api:synthesis")
    return redirect(request.referrer or url_for("admin.synthesis_list"))


@bp.post("/synthesis/<int:report_id>/resynthesize")
@login_required
def synthesis_rerun(report_id: int):
    report = db.session.get(SynthesizedReport, report_id) or abort(404)
    report.llm_status = "pending"
    report.llm_attempts = 0
    report.llm_claimed_at = None
    audit(current_actor(), "synthesis.rerun", entity="synthesized_reports",
          entity_id=str(report_id))
    db.session.commit()
    from celery_worker.tasks.synthesize import synthesize_reports_batch

    try:
        synthesize_reports_batch.delay(report_ids=[report_id])
        flash("Re-synthesis queued.", "ok")
    except Exception as exc:
        flash(f"Marked pending; could not reach the broker ({exc}).", "warn")
    return redirect(request.referrer or url_for("admin.synthesis_list"))
