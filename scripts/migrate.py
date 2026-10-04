from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings, validate_runtime_settings
from app.db import apply_migrations, assert_migrations_current, connect
from app.services import seed_database


def main() -> None:
    validate_runtime_settings(settings)
    applied = apply_migrations()
    db = connect()
    try:
        seed_database(db, settings)
        assert_migrations_current(db)
    finally:
        db.close()
    if applied:
        print("Applied database migrations: " + ", ".join(applied))
    else:
        print("Database schema is already at the current migration version.")
    print("BizFlow plan and platform configuration seed completed.")
    if not settings.is_deployment:
        print("If this is a new database, sign in with the administrator credentials entered during local setup.")
    else:
        print("For first-time bootstrap, remove ADMIN_PASSWORD from the migration environment after the administrator is created.")


if __name__ == "__main__":
    main()
