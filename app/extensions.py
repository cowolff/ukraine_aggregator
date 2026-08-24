"""Singletons shared by the web tier and the workers.

The Celery instance lives in ``celery_worker.celery_app`` (PLAN §10 — one instance, not two);
this module re-exports it so Flask code never constructs a second one.
"""
from __future__ import annotations

import logging

import redis as redis_lib
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

# Naming convention keeps alembic autogenerate diffs stable.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


from flask_sqlalchemy import SQLAlchemy  # noqa: E402  (import after Base for model_class)

db = SQLAlchemy(model_class=Base)
login_manager = LoginManager()
csrf = CSRFProtect()

redis_client = redis_lib.Redis.from_url(settings.redis_url, decode_responses=False)

log = logging.getLogger("ukraine")


def worker_session_factory():
    """Session factory for Celery tasks, which run outside the Flask app context."""
    from sqlalchemy import create_engine

    engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=3, max_overflow=2)
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_celery():
    from celery_worker.celery_app import celery

    return celery
