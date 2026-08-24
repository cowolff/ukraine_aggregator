"""Pytest fixtures. The suite needs Postgres+PostGIS and Redis; it skips cleanly without them.

Locally, `docker compose up -d postgis redis` plus the dev override publishes them on
localhost:55432 / localhost:56379, which is what TEST_DATABASE_URL defaults to.
LLM calls are always mocked — no test ever reaches the proxy.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://ukraine:ukraine@localhost:55432/ukraine_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:56379/1")
FIXTURES = ROOT / "tests" / "fixtures"

# Point the app at the test instances before app.config is first imported.
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["TESTING"] = "1"
os.environ.setdefault("LITELLM_API_BASE", "https://proxy.invalid/v1")
os.environ.setdefault("LITELLM_MODEL", "test-model")
os.environ.setdefault("LITELLM_API_KEY", "test-key")

TABLES = [
    "evidence_links", "frontline_claims", "frontline_snapshots", "upstream_geometries",
    "extracted_events", "news_items", "sources", "gazetteer", "blackout_zones",
    "notifications", "audit_log", "admin_users",
]


def _admin_url() -> str:
    return TEST_DB_URL.rsplit("/", 1)[0] + "/postgres"


def _database_name() -> str:
    return TEST_DB_URL.rsplit("/", 1)[1]


def _ensure_database() -> str | None:
    """Create the test database if missing. Returns a skip reason, or None on success."""
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return "psycopg not installed"
    dsn = _admin_url().replace("postgresql+psycopg://", "postgresql://")
    try:
        with psycopg.connect(dsn, connect_timeout=4, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (_database_name(),)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{_database_name()}"')
    except Exception as exc:
        return f"Postgres unavailable at {dsn}: {exc}"
    return None


def _ensure_redis() -> str | None:
    try:
        import redis

        redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=3).ping()
    except Exception as exc:
        return f"Redis unavailable at {TEST_REDIS_URL}: {exc}"
    return None


@pytest.fixture(scope="session")
def _infra():
    for reason in (_ensure_database(), _ensure_redis()):
        if reason:
            pytest.skip(reason, allow_module_level=True)
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(config, "head")
    return True


@pytest.fixture(scope="session")
def app(_infra):
    from app import create_app

    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})


@pytest.fixture(autouse=True)
def app_context(app):
    """A fresh app context per test.

    This must not be session-scoped: Flask reuses an already-pushed app context for incoming
    test-client requests, so a session-wide one would share `g` — and therefore Flask-Login's
    cached `g._login_user` — across every test in the run.
    """
    with app.app_context():
        yield


@pytest.fixture(autouse=True)
def clean_db(app, app_context):
    """Truncate between tests: geometry work needs real commits, so no outer-transaction trick."""
    from sqlalchemy import text

    from app.extensions import db, redis_client

    db.session.rollback()
    db.session.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    db.session.commit()
    db.session.expire_all()   # identity map must not survive a RESTART IDENTITY
    try:
        redis_client.flushdb()
    except Exception:
        pass
    yield
    db.session.rollback()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db_session(app):
    from app.extensions import db

    return db.session


# --------------------------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------------------------
@pytest.fixture
def make_source(db_session):
    counter = {"n": 0}

    def _make(perspective="ukrainian", *, tier=2, name=None, type_="rss", enabled=True, meta=None):
        from app.models import Source

        counter["n"] += 1
        source = Source(
            name=name or f"{perspective} source {counter['n']}",
            type=type_,
            url=f"https://example.test/{perspective}/{counter['n']}",
            perspective=perspective,
            reliability_tier=tier,
            enabled=enabled,
            meta=meta or {},
        )
        db_session.add(source)
        db_session.commit()
        return source

    return _make


@pytest.fixture
def make_event(db_session):
    counter = {"n": 0}

    def _make(
        source,
        *,
        event_type="frontline_advance",
        lat=48.59,
        lon=37.83,
        claimed_by="ru",
        confidence=0.9,
        title=None,
        url=None,
        gazetteer_id=None,
        published_at=None,
        occurred_at=None,
        debunk_target=None,
    ):
        from sqlalchemy import text

        from app.models import ExtractedEvent, NewsItem, content_hash

        counter["n"] += 1
        title = title or f"report {counter['n']} from {source.name}"
        item = NewsItem(
            source_id=source.id,
            external_id=f"ext-{counter['n']}",
            content_hash=content_hash(title, str(counter["n"])),
            title=title,
            body=f"body {counter['n']}",
            url=url,
            published_at=published_at or dt.datetime.now(dt.timezone.utc),
            llm_status="done",
        )
        db_session.add(item)
        db_session.flush()
        event = ExtractedEvent(
            news_item_id=item.id,
            event_type=event_type,
            claimed_by=claimed_by,
            confidence=confidence,
            occurred_at=occurred_at or item.published_at,
            gazetteer_id=gazetteer_id,
            coord_source="explicit_coords" if lat is not None else None,
            llm_raw={"debunk_target": debunk_target} if debunk_target else {},
        )
        db_session.add(event)
        db_session.flush()
        if lat is not None and lon is not None:
            db_session.execute(
                text(
                    "UPDATE extracted_events SET geom = ST_SetSRID(ST_MakePoint(:lon,:lat),4326) "
                    "WHERE id = :eid"
                ),
                {"lon": lon, "lat": lat, "eid": event.id},
            )
        db_session.commit()
        db_session.refresh(event)
        return event

    return _make


@pytest.fixture
def make_snapshot(db_session):
    """Insert a baseline RU snapshot so distance-to-line and inside/outside tests have a line."""

    # The eastern edge (lon 37.9) runs ~5 km east of Chasiv Yar, so the scenario points sit
    # inside RU control but well within DEEP_STRIKE_KM of the line — which is what the
    # corroboration and geo-proof rules are about. Kyiv stays hundreds of km behind it.
    def _make(wkt="MULTIPOLYGON(((37.0 48.0, 37.9 48.0, 37.9 49.0, 37.0 49.0, 37.0 48.0)))"):
        from sqlalchemy import text

        db_session.execute(
            text(
                "INSERT INTO frontline_snapshots (layer, geom, simplified, generation_meta) "
                "VALUES ('ru', ST_GeomFromText(:wkt, 4326), '{}'::jsonb, '{}'::jsonb)"
            ),
            {"wkt": wkt},
        )
        db_session.commit()

    return _make


@pytest.fixture
def gazetteer(db_session):
    """Load the offline gazetteer fixture."""
    import orjson
    from sqlalchemy import text

    from app.services.geocode import name_search_value

    places = orjson.loads((FIXTURES / "gazetteer_fixture.json").read_bytes())
    ids = {}
    for place in places:
        row = db_session.execute(
            text(
                "INSERT INTO gazetteer (name_uk, name_ru, name_en, name_search, oblast, "
                "population, geom) VALUES (:uk, :ru, :en, :search, :oblast, :pop, "
                "ST_SetSRID(ST_MakePoint(:lon,:lat),4326)) RETURNING id"
            ),
            {
                "uk": place["name_uk"], "ru": place["name_ru"], "en": place["name_en"],
                "search": name_search_value(place["name_uk"], place["name_ru"], place["name_en"]),
                "oblast": place["oblast"], "pop": place["population"],
                "lat": place["lat"], "lon": place["lon"],
            },
        ).first()
        ids.setdefault(place["name_en"], []).append(row.id)
    db_session.commit()
    return ids
