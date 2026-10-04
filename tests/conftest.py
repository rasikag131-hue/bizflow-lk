from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_test_secrets(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "session_secret", "test-session-secret-not-for-deployment-2026")
    monkeypatch.setattr(settings, "pii_master_key", "test-pii-master-key-not-for-deployment-2026")
    monkeypatch.setattr(settings, "admin_email", "admin@example.test")
    monkeypatch.setattr(settings, "admin_password", "Test-Admin-Passphrase-Only-2026!")
