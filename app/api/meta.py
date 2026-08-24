from __future__ import annotations

import datetime as dt

from sqlalchemy import or_, select, text

from app.api import bp, cached_json, json_response
from app.config import settings
from app.extensions import db
from app.models import EVENT_GLYPHS, EVENT_TYPES, PERSPECTIVES, Notification

PERSPECTIVE_COLORS = {
    "ukrainian": "#0057B7",
    "russian": "#D52B1E",
    "western": "#2E7D32",
    "neutral": "#757575",
}


@bp.get("/config")
def config():
    return json_response(
        {
            "poll_seconds": settings.public_poll_seconds,
            "map_defaults": {"center": [31.0, 48.5], "zoom": 6},
            "event_types": list(EVENT_TYPES),
            "perspectives": list(PERSPECTIVES),
            "glyphs": EVENT_GLYPHS,
            "colors": PERSPECTIVE_COLORS,
            "detail_tiers": list(settings.snapshot_simplify_tolerances.keys()),
            "low_confidence_floor": settings.low_confidence_floor,
        },
        max_age=300,
    )


@bp.get("/timeline")
def timeline():
    """Bounds and coverage for the time scrubber.

    Returns the span the data actually supports, the instants a frontline snapshot exists for
    (so the UI can snap to real geometry rather than interpolating), and per-day event counts.
    """

    def build():
        bounds = db.session.execute(
            text(
                """
                SELECT
                  (SELECT min(valid_at) FROM frontline_snapshots WHERE layer = 'ru') AS geom_from,
                  (SELECT max(valid_at) FROM frontline_snapshots WHERE layer = 'ru') AS geom_to,
                  (SELECT min(occurred_at) FROM extracted_events WHERE geom IS NOT NULL) AS ev_min,
                  (SELECT max(occurred_at) FROM extracted_events WHERE geom IS NOT NULL) AS ev_to,
                  -- A handful of feeds still carry years-old articles. Anchoring the scrubber on
                  -- the absolute minimum lets one such item stretch the track across empty years,
                  -- so the usable start is a low percentile instead.
                  (SELECT percentile_disc(0.01) WITHIN GROUP (ORDER BY occurred_at)
                     FROM extracted_events WHERE geom IS NOT NULL) AS ev_from
                """
            )
        ).mappings().first()

        snapshots = [
            row.valid_at.isoformat()
            for row in db.session.execute(
                text(
                    "SELECT valid_at FROM frontline_snapshots WHERE layer = 'ru' "
                    "ORDER BY valid_at"
                )
            ).all()
        ]
        histogram = [
            {"day": row.day.isoformat(), "events": row.n}
            for row in db.session.execute(
                text(
                    """
                    SELECT date_trunc('day', occurred_at)::date AS day, count(*) AS n
                    FROM extracted_events
                    WHERE geom IS NOT NULL AND occurred_at > now() - interval '400 days'
                    GROUP BY 1 ORDER BY 1
                    """
                )
            ).all()
        ]
        starts = [b for b in (bounds["geom_from"], bounds["ev_from"]) if b]
        ends = [b for b in (bounds["geom_to"], bounds["ev_to"]) if b]
        return {
            "from": min(starts).isoformat() if starts else None,
            "to": max(ends).isoformat() if ends else None,
            # The true extent, for transparency: any instant remains addressable via ?at=.
            "events_from": bounds["ev_min"].isoformat() if bounds["ev_min"] else None,
            "frontline_from": bounds["geom_from"].isoformat() if bounds["geom_from"] else None,
            "frontline_to": bounds["geom_to"].isoformat() if bounds["geom_to"] else None,
            "snapshot_count": len(snapshots),
            "snapshots": snapshots,
            "events_per_day": histogram,
        }

    return cached_json("timeline", {}, build, ttl=300)


@bp.get("/notifications")
def notifications():
    def build():
        now = dt.datetime.now(dt.timezone.utc)
        rows = (
            db.session.execute(
                select(Notification)
                .where(
                    Notification.active.is_(True),
                    or_(Notification.starts_at.is_(None), Notification.starts_at <= now),
                    or_(Notification.ends_at.is_(None), Notification.ends_at >= now),
                )
                .order_by(Notification.id.desc())
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "id": n.id,
                    "title": n.title,
                    "body": n.body,
                    "level": n.level,
                    "starts_at": n.starts_at.isoformat() if n.starts_at else None,
                    "ends_at": n.ends_at.isoformat() if n.ends_at else None,
                }
                for n in rows
            ]
        }

    return cached_json("notifications", {}, build, ttl=60)
