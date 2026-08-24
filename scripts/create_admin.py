#!/usr/bin/env python
"""Create or update the admin user (PLAN §19.1). Idempotent.

Credentials come from ADMIN_USERNAME / ADMIN_PASSWORD, or from --username/--password.
The password is bcrypt-hashed into the DB; it is never stored in plaintext.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app import create_app
from app.config import settings
from app.extensions import db
from app.models import AdminUser
from app.services.audit import audit

MIN_PASSWORD_LEN = 10


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default=settings.admin_username or None)
    parser.add_argument("--password", default=settings.admin_password or None)
    parser.add_argument("--prompt", action="store_true", help="ask for the password interactively")
    args = parser.parse_args()

    username = args.username
    if not username:
        print("error: no username (set ADMIN_USERNAME or pass --username)", file=sys.stderr)
        return 1
    password = getpass.getpass("Password: ") if args.prompt else args.password
    if not password:
        print("error: no password (set ADMIN_PASSWORD, --password or --prompt)", file=sys.stderr)
        return 1
    if len(password) < MIN_PASSWORD_LEN:
        print(f"error: password must be at least {MIN_PASSWORD_LEN} characters", file=sys.stderr)
        return 1

    app = create_app()
    with app.app_context():
        user = db.session.execute(
            select(AdminUser).where(AdminUser.username == username)
        ).scalar_one_or_none()
        action = "admin.update" if user else "admin.create"
        if user is None:
            user = AdminUser(username=username, password_hash=AdminUser.hash_password(password))
            db.session.add(user)
        else:
            user.password_hash = AdminUser.hash_password(password)
        audit("script:create_admin", action, entity="admin_users", entity_id=username)
        db.session.commit()
    print(f"{'updated' if action == 'admin.update' else 'created'} admin user {username!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
