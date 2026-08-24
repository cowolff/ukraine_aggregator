"""Flask application factory (PLAN §5)."""
from __future__ import annotations

import logging
import sys

from flask import Flask, jsonify, send_from_directory

from app.config import SECRET_KEY_IS_EPHEMERAL, settings
from app.extensions import csrf, db, login_manager, redis_client


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('{"ts":"%(asctime)s","lvl":"%(levelname)s","logger":"%(name)s","msg":%(message)r}')
    )
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def create_app(config_overrides: dict | None = None) -> Flask:
    _configure_logging()
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.update(settings.flask_config())
    if config_overrides:
        app.config.update(config_overrides)

    if SECRET_KEY_IS_EPHEMERAL and not app.config.get("TESTING"):
        app.logger.warning("SECRET_KEY unset — generated an ephemeral one; sessions die on restart")

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "admin.login"
    login_manager.session_protection = "strong"

    from app import models as _models  # noqa: F401  (populate the mapper registry)
    from app.models import AdminUser

    @login_manager.user_loader
    def load_user(user_id: str):  # pragma: no cover - exercised via admin tests
        return db.session.get(AdminUser, int(user_id))

    from app.admin import bp as admin_bp
    from app.api import bp as api_bp

    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)
    csrf.exempt(api_bp)  # public API is read-only and unauthenticated

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/healthz")
    def healthz():
        """Liveness + a little operational truth. No auth, no secrets (PLAN §15)."""
        from app.services.health import health_report

        report = health_report()
        return jsonify(report), (200 if report["db"] == "ok" and report["redis"] == "ok" else 503)

    @app.errorhandler(404)
    def not_found(_e):
        return jsonify({"error": "not_found"}), 404

    @app.teardown_appcontext
    def _shutdown_session(exc=None):  # pragma: no cover
        if exc:
            db.session.rollback()
        db.session.remove()

    app.extensions["redis"] = redis_client
    return app
