"""Session-authenticated admin backend (PLAN §17)."""
from __future__ import annotations

from flask import Blueprint

bp = Blueprint("admin", __name__, url_prefix="/admin", template_folder="templates")

from app.admin import auth, views  # noqa: E402,F401  (register routes)
