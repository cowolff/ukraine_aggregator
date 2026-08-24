"""All ORM models. Import this module (never the submodules piecemeal) so that
SQLAlchemy's registry is complete before any mapper is configured.
"""
from app.models.admin import (  # noqa: F401
    NOTIFICATION_LEVELS,
    AdminUser,
    AuditLog,
    BlackoutZone,
    Notification,
)
from app.models.events import (  # noqa: F401
    COORD_SOURCES,
    EVENT_GLYPHS,
    EVENT_TYPES,
    FRONTLINE_EVENT_TYPES,
    ExtractedEvent,
)
from app.models.frontline import (  # noqa: F401
    CLAIM_DIRECTIONS,
    CLAIM_STATUSES,
    EVIDENCE_ROLES,
    SNAPSHOT_LAYERS,
    EvidenceLink,
    FrontlineClaim,
    FrontlineSnapshot,
    UpstreamGeometry,
)
from app.models.gazetteer import GazetteerEntry  # noqa: F401
from app.models.news import LLM_STATUSES, NewsItem, content_hash  # noqa: F401
from app.models.sources import PERSPECTIVES, SOURCE_TYPES, JSONType, Source  # noqa: F401

__all__ = [
    "AdminUser", "AuditLog", "BlackoutZone", "Notification", "ExtractedEvent",
    "EvidenceLink", "FrontlineClaim", "FrontlineSnapshot", "UpstreamGeometry",
    "GazetteerEntry", "NewsItem", "Source", "content_hash",
    "PERSPECTIVES", "SOURCE_TYPES", "EVENT_TYPES", "EVENT_GLYPHS", "FRONTLINE_EVENT_TYPES",
    "COORD_SOURCES", "LLM_STATUSES", "CLAIM_DIRECTIONS", "CLAIM_STATUSES", "EVIDENCE_ROLES",
    "SNAPSHOT_LAYERS", "NOTIFICATION_LEVELS", "JSONType",
]
