"""One Flask app per worker process, so tasks can use the Flask-SQLAlchemy session."""
from __future__ import annotations

import functools

_app = None


def get_app():
    global _app
    if _app is None:
        from app import create_app

        _app = create_app()
    return _app


def with_app_context(func):
    """Decorator: run a task body inside the worker's Flask app context."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        app = get_app()
        with app.app_context():
            from app.extensions import db

            try:
                return func(*args, **kwargs)
            except Exception:
                db.session.rollback()
                raise
            finally:
                db.session.remove()

    return wrapper
