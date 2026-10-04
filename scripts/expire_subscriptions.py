from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings, validate_runtime_settings
from app.db import assert_migrations_current, connect
from app.services import run_billing_jobs


def main() -> None:
    validate_runtime_settings(settings)
    db = connect()
    try:
        assert_migrations_current(db)
        result = run_billing_jobs(db)
    finally:
        db.close()
    print(f"Subscription job complete. Expired: {result['expired']}; warnings added: {result['warnings']}.")


if __name__ == "__main__":
    main()
