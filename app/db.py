from __future__ import annotations

import importlib.util
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import ROOT, settings

SQLITE_SCHEMA = ROOT / "database" / "schema.sqlite.sql"
POSTGRES_SCHEMA = ROOT / "database" / "schema.postgres.sql"
MIGRATIONS_DIR = ROOT / "database" / "migrations"


class PgCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    @staticmethod
    def _normalise(row: Any) -> Any:
        if row is None:
            return None
        return {key: (value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value)
                for key, value in row.items()}

    def fetchone(self) -> Any:
        return self._normalise(self._cursor.fetchone())

    def fetchall(self) -> list[Any]:
        return [self._normalise(row) for row in self._cursor.fetchall()]


class PgConnection:
    dialect = "postgres"

    def __init__(self, raw: Any):
        self.raw = raw

    @staticmethod
    def _convert_value(value: Any) -> Any:
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    def execute(self, query: str, params: Any = ()) -> PgCursor:
        # SQL in the shared service layer uses qmark placeholders; psycopg uses %s.
        converted = tuple(self._convert_value(v) for v in params)
        cursor = self.raw.execute(query.replace("?", "%s"), converted)
        return PgCursor(cursor)

    def close(self) -> None:
        self.raw.close()


def is_postgres() -> bool:
    return settings.database_url.startswith(("postgres://", "postgresql://"))


def _sqlite_path() -> Path:
    url = settings.database_url
    if url == "sqlite:///:memory:":
        return Path(":memory:")
    if url.startswith("sqlite:///"):
        path = Path(url[len("sqlite:///"):])
        return path if path.is_absolute() else ROOT / path
    raise RuntimeError("DATABASE_URL must be sqlite:///... or a PostgreSQL URL.")


def connect() -> Any:
    if is_postgres():
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL support requires psycopg. Run pip install -r requirements.txt.") from exc
        raw = psycopg.connect(settings.database_url, row_factory=dict_row, autocommit=True)
        return PgConnection(raw)
    path = _sqlite_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path), timeout=30, check_same_thread=False, isolation_level=None)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        raw.execute("PRAGMA journal_mode = WAL")
    return raw


def execute_script(db: Any, script: str) -> None:
    if getattr(db, "dialect", "sqlite") == "postgres":
        # Current schema has no procedural blocks or semicolons in string literals.
        for statement in script.split(";"):
            if statement.strip():
                db.execute(statement)
    else:
        db.executescript(script)


def _migration_files() -> list[Path]:
    return sorted(path for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.py") if path.is_file())


def _migration_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"bizflow_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("A database migration could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "upgrade", None)):
        raise RuntimeError("A database migration is missing its upgrade(db) function.")
    return module


def _ensure_migration_table(db: Any) -> None:
    applied_at_type = "TIMESTAMPTZ" if getattr(db, "dialect", "sqlite") == "postgres" else "TEXT"
    db.execute(f"CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at {applied_at_type} NOT NULL)")


def apply_initial_schema(db: Any) -> None:
    """Idempotent baseline/compatibility upgrade, kept behind migration 0001."""
    postgres = getattr(db, "dialect", "sqlite") == "postgres"
    schema = POSTGRES_SCHEMA if postgres else SQLITE_SCHEMA
    execute_script(db, schema.read_text(encoding="utf-8"))

    # Additive compatibility migration for older BizFlow databases. Customer business
    # records are preserved; identity values are never guessed or logged.
    user_columns = {
        "public_account_id": "TEXT",
        "identity_number_encrypted": "TEXT",
        "identity_number_lookup_hash": "TEXT",
        "last_login_at": "TIMESTAMPTZ" if postgres else "TEXT",
        "password_changed_at": "TIMESTAMPTZ" if postgres else "TEXT",
    }
    if postgres:
        for column, data_type in user_columns.items():
            db.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {column} {data_type}")
    else:
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        for column, data_type in user_columns.items():
            if column not in columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {column} {data_type}")

    # Retire old password-reset/provider-link schema without using any provider auth.
    db.execute("DROP TABLE IF EXISTS password_reset_tokens")
    db.execute("DROP TABLE IF EXISTS google_link_intents")
    if postgres:
        db.execute("ALTER TABLE users DROP COLUMN IF EXISTS google_sub")
    else:
        legacy_columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        if "google_sub" in legacy_columns:
            # Old SQLite schemas declared this retired identifier UNIQUE; clear it but
            # preserve the legacy table layout so existing foreign keys remain intact.
            db.execute("UPDATE users SET google_sub=NULL")

    from app.account_security import backfill_account_identifiers
    backfill_account_identifiers(db)

    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_public_account_unique ON users(public_account_id)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_ci ON users(lower(username))")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_ci ON users(lower(email))")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_identity_lookup_unique ON users(identity_number_lookup_hash)")
    if postgres:
        db.execute("ALTER TABLE users ALTER COLUMN username SET NOT NULL")
        db.execute("ALTER TABLE users ALTER COLUMN public_account_id SET NOT NULL")
    else:
        db.execute("CREATE TRIGGER IF NOT EXISTS user_identifiers_required_insert BEFORE INSERT ON users "
                   "WHEN NEW.username IS NULL OR trim(NEW.username)='' OR NEW.public_account_id IS NULL OR trim(NEW.public_account_id)='' "
                   "BEGIN SELECT RAISE(ABORT,'username and public account ID are required'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS user_identifiers_required_update BEFORE UPDATE OF username,public_account_id ON users "
                   "WHEN NEW.username IS NULL OR trim(NEW.username)='' OR NEW.public_account_id IS NULL OR trim(NEW.public_account_id)='' "
                   "BEGIN SELECT RAISE(ABORT,'username and public account ID are required'); END")


def apply_migrations() -> list[str]:
    """Apply numbered migrations once; production deployments run this explicitly."""
    db = connect()
    applied_now: list[str] = []
    try:
        _ensure_migration_table(db)
        files = _migration_files()
        known = {path.stem for path in files}
        recorded = {row["version"] for row in db.execute("SELECT version FROM schema_migrations").fetchall()}
        unknown = recorded - known
        if unknown:
            raise RuntimeError("The database contains migration versions that this application build does not recognize.")
        for path in files:
            version = path.stem
            if version in recorded:
                continue
            _migration_module(path).upgrade(db)
            with transaction(db):
                db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (version, _now_text()))
            applied_now.append(version)
        return applied_now
    finally:
        db.close()


def assert_migrations_current(db: Any | None = None) -> None:
    """Read-only production-startup check; it never creates or alters schema."""
    owned = db is None
    connection = db or connect()
    try:
        expected = {path.stem for path in _migration_files()}
        try:
            recorded = {row["version"] for row in connection.execute("SELECT version FROM schema_migrations").fetchall()}
        except Exception as exc:
            raise RuntimeError("Database migrations are not recorded. Run `python scripts/migrate.py` before starting the API.") from exc
        missing = expected - recorded
        unknown = recorded - expected
        if missing:
            raise RuntimeError("Database migrations are pending. Run `python scripts/migrate.py` before starting the API.")
        if unknown:
            raise RuntimeError("The database contains migration versions not recognized by this application build.")
    finally:
        if owned:
            connection.close()


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def init_db() -> None:
    """Compatibility entry point for local development and tests only."""
    if settings.app_env in {"staging", "production"}:
        raise RuntimeError("Staging/production schema changes must be run explicitly with `python scripts/migrate.py`.")
    apply_migrations()


def get_db() -> Iterator[Any]:
    db = connect()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def transaction(db: Any) -> Iterator[None]:
    dialect = getattr(db, "dialect", "sqlite")
    db.execute("BEGIN" if dialect == "postgres" else "BEGIN IMMEDIATE")
    try:
        yield
        if dialect == "postgres":
            db.execute("COMMIT")
        else:
            db.commit()
    except Exception:
        if dialect == "postgres":
            db.execute("ROLLBACK")
        else:
            db.rollback()
        raise


def rowdict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
