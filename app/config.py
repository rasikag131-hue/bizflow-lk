from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

# Small local .env loader; the process environment always takes precedence.
def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _https_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and bool(host) and parsed.username is None and parsed.password is None
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                and host not in {"localhost", "127.0.0.1", "0.0.0.0"})
    except ValueError:
        return False


_load_dotenv()


class Settings:
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    port = int(os.getenv("PORT", "8000"))
    database_url = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'bizflow.db'}").strip()

    # SESSION_SECRET is the HMAC key used to derive CSRF tokens. JWT_SECRET is read only
    # as a development compatibility alias; deployments should migrate to SESSION_SECRET.
    session_secret_explicit = bool(os.getenv("SESSION_SECRET", "").strip())
    session_secret = os.getenv("SESSION_SECRET", "").strip() or os.getenv("JWT_SECRET", "").strip()
    pii_master_key = os.getenv("PII_MASTER_KEY", "").strip()
    identity_number_regex = os.getenv("IDENTITY_NUMBER_REGEX", r"(?:[0-9]{9}[VX]|[0-9]{12})")

    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_password = os.getenv("ADMIN_PASSWORD", "")

    storage_dir_value = os.getenv("STORAGE_DIR", "").strip()
    storage_dir_explicit = bool(storage_dir_value)
    storage_dir = Path(storage_dir_value or str(ROOT / "data" / "private")).expanduser()
    max_receipt_mb = max(1, min(int(os.getenv("MAX_RECEIPT_MB", "5")), 15))
    default_support_phone = ""

    frontend_url = os.getenv("FRONTEND_URL", "").strip().rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_deployment(self) -> bool:
        return self.app_env in {"staging", "production"}


def validate_runtime_settings(settings: Settings) -> None:
    if settings.app_env not in {"development", "test", "preview", "staging", "production"}:
        raise RuntimeError("APP_ENV must be development, test, preview, staging, or production.")
    if len(settings.session_secret) < 32 or "change-me" in settings.session_secret.lower():
        raise RuntimeError("Set SESSION_SECRET to a random secret of at least 32 characters.")
    try:
        re.compile(settings.identity_number_regex, re.IGNORECASE)
    except re.error as exc:
        raise RuntimeError("IDENTITY_NUMBER_REGEX must be a valid regular expression.") from exc

    if not settings.is_deployment:
        return
    if not settings.session_secret_explicit:
        raise RuntimeError("Set SESSION_SECRET explicitly for staging/production; the legacy JWT_SECRET alias is development-only.")
    if len(settings.pii_master_key) < 32 or "change-me" in settings.pii_master_key.lower():
        raise RuntimeError("Set PII_MASTER_KEY to a separate random secret of at least 32 characters.")
    if settings.pii_master_key == settings.session_secret:
        raise RuntimeError("PII_MASTER_KEY must be different from SESSION_SECRET.")
    if not settings.database_url.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("Staging/production must use PostgreSQL. SQLite is for local development and tests only.")
    if not settings.frontend_url or not _https_origin(settings.frontend_url):
        raise RuntimeError("Set FRONTEND_URL to the real HTTPS frontend origin before staging/production startup.")
    if not settings.storage_dir_explicit or not settings.storage_dir.is_absolute():
        raise RuntimeError("Set STORAGE_DIR to an absolute path on a persistent mounted volume.")
    try:
        storage = settings.storage_dir.resolve()
        root = ROOT.resolve()
    except OSError as exc:
        raise RuntimeError("STORAGE_DIR could not be resolved.") from exc
    if storage == root or root in storage.parents:
        raise RuntimeError("STORAGE_DIR must be outside the application source directory on persistent storage.")


settings = Settings()
