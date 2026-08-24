"""Admin login with a Redis-backed 5/min/IP rate limit (PLAN §17)."""
from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.admin import bp
from app.admin.forms import LoginForm
from app.extensions import db, redis_client
from app.models import AdminUser
from app.services.audit import audit

LOGIN_LIMIT = 5
LOGIN_WINDOW_S = 60


def _rate_limited() -> bool:
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    key = f"ratelimit:login:{ip}"
    try:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, LOGIN_WINDOW_S)
        return count > LOGIN_LIMIT
    except Exception:
        return False  # Redis down must not lock admins out entirely


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        if _rate_limited():
            flash("Too many attempts. Wait a minute.", "error")
            return render_template("login.html", form=form), 429
        user = db.session.execute(
            db.select(AdminUser).where(AdminUser.username == form.username.data)
        ).scalar_one_or_none()
        if user and user.check_password(form.password.data):
            login_user(user)
            audit(f"admin:{user.username}", "auth.login", commit=True)
            return redirect(request.args.get("next") or url_for("admin.dashboard"))
        audit("anonymous", "auth.login_failed", detail={"username": form.username.data}, commit=True)
        flash("Invalid credentials.", "error")
    return render_template("login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    audit(f"admin:{current_user.username}", "auth.logout", commit=True)
    logout_user()
    return redirect(url_for("admin.login"))
