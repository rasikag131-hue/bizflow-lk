from __future__ import annotations

import getpass
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ENV_FILE = ROOT / ".env"
TEMPLATE = ROOT / ".env.example"


def _read() -> tuple[list[str], dict[str, str]]:
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines() if not ENV_FILE.exists() else ENV_FILE.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return lines, values


def _set(lines: list[str], key: str, value: str) -> list[str]:
    prefix = key + "="
    for index, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[index] = prefix + value
            return lines
    lines.append(prefix + value)
    return lines


def _sqlite_path(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite:///") or database_url == "sqlite:///:memory:":
        return None
    path = Path(database_url[len("sqlite:///"):]).expanduser()
    return path if path.is_absolute() else ROOT / path


def _local_database_has_identity_ciphertext(database_url: str) -> bool:
    path = _sqlite_path(database_url)
    if not path or not path.is_file():
        return False
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
            return "identity_number_encrypted" in columns and bool(db.execute(
                "SELECT 1 FROM users WHERE identity_number_encrypted IS NOT NULL LIMIT 1"
            ).fetchone())
        finally:
            db.close()
    except sqlite3.Error:
        return False


def _local_database_has_admin(database_url: str) -> bool:
    path = _sqlite_path(database_url)
    if not path or not path.is_file():
        return False
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            return bool(exists and db.execute("SELECT 1 FROM users WHERE role='SUPER_ADMIN' LIMIT 1").fetchone())
        finally:
            db.close()
    except sqlite3.Error:
        return False


def _admin_credentials(values: dict[str, str]) -> tuple[str, str]:
    email = values.get("ADMIN_EMAIL", "").strip().lower()
    while not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        email = input("Initial Super Admin email: ").strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            print("Enter a valid email address.")
    while True:
        password = getpass.getpass("Initial Super Admin password (14-128 characters): ")
        if len(password) < 14 or len(password) > 128 or password != password.strip() or password[:1] in {'"', "'"}:
            print("Use 14-128 characters without leading/trailing whitespace or a leading quote.")
            continue
        confirmation = getpass.getpass("Confirm the password: ")
        if password == confirmation:
            return email, password
        print("The passwords did not match.")


def main() -> None:
    lines, values = _read()
    for key in ("APP_ENV", "DATABASE_URL", "SESSION_SECRET", "PII_MASTER_KEY", "ADMIN_EMAIL", "STORAGE_DIR", "FRONTEND_URL", "API_URL", "JWT_SECRET"):
        if os.getenv(key):
            values[key] = os.environ[key]
    active_env = os.getenv("APP_ENV", values.get("APP_ENV", "")).strip().lower()
    if active_env in {"production", "staging"}:
        raise SystemExit("setup_local.py is for local development only; it will not change a deployment environment.")

    defaults = {
        "APP_ENV": "development",
        "DATABASE_URL": "sqlite:///./data/bizflow.db",
        "STORAGE_DIR": "./data/private",
    }
    for key, value in defaults.items():
        if not values.get(key):
            values[key] = value
    for key in defaults:
        lines = _set(lines, key, values[key])

    # Preserve local PII derivation before replacing a weak/legacy session secret.
    previous_secret = values.get("JWT_SECRET") or values.get("SESSION_SECRET", "")
    effective_database = os.getenv("DATABASE_URL", values.get("DATABASE_URL", ""))
    if (not values.get("PII_MASTER_KEY") and not previous_secret
            and _local_database_has_identity_ciphertext(effective_database)):
        raise SystemExit("This local database contains encrypted identity data but no recoverable local key was found. Restore the original PII_MASTER_KEY before starting; do not generate a replacement key.")
    if not values.get("PII_MASTER_KEY") and previous_secret:
        preserved_pii_key = previous_secret + "|bizflow-development-pii-key"
        values["PII_MASTER_KEY"] = preserved_pii_key
    if values.get("PII_MASTER_KEY"):
        lines = _set(lines, "PII_MASTER_KEY", values["PII_MASTER_KEY"])
    session_secret = values.get("SESSION_SECRET") or values.get("JWT_SECRET") or ""
    if len(session_secret) < 32 or "change-me" in session_secret.lower():
        session_secret = secrets.token_urlsafe(48)
    values["SESSION_SECRET"] = session_secret
    lines = _set(lines, "SESSION_SECRET", session_secret)

    if _local_database_has_admin(effective_database):
        bootstrap_password = ""
        print("An existing local Super Admin was found; local setup will not reset that account's password.")
    else:
        email, bootstrap_password = _admin_credentials(values)
        values["ADMIN_EMAIL"] = email
        lines = _set(lines, "ADMIN_EMAIL", email)
    # A bootstrap password exists in this process only. It is never written to .env.
    lines = _set(lines, "ADMIN_PASSWORD", "")
    ENV_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass

    for key, value in values.items():
        if key != "ADMIN_PASSWORD":
            os.environ.setdefault(key, value)
    os.environ["ADMIN_PASSWORD"] = bootstrap_password

    from app.config import settings, validate_runtime_settings
    from app.db import apply_migrations, assert_migrations_current, connect
    from app.services import seed_database

    validate_runtime_settings(settings)
    applied = apply_migrations()
    db = connect()
    try:
        seed_database(db, settings)
        assert_migrations_current(db)
    finally:
        db.close()

    print("Local environment, schema, and administrator bootstrap are ready. The initial password was hashed and was not saved to .env.")
    if applied:
        print("Applied migrations: " + ", ".join(applied))


if __name__ == "__main__":
    main()
