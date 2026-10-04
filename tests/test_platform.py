from __future__ import annotations

import hashlib
import io
from datetime import timedelta
from pathlib import Path
import stat

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.account_security import decrypt_identity_number
from app.config import settings
from app.db import connect, transaction
from app.main import app
from app.security import (digest_secret, hash_password, iso_utc, parse_dt, password_needs_rehash,
                          utc_now, verify_password)
from app.services import run_billing_jobs


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'bizflow-test.sqlite3'}")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "private")
    monkeypatch.setattr(settings, "app_env", "test")
    with TestClient(app) as test_client:
        yield test_client


def _identity_for(email: str) -> str:
    digits = str(int(hashlib.sha256(email.lower().encode()).hexdigest()[:8], 16) % 1_000_000_000).zfill(9)
    return f"{digits}V"


def register(client: TestClient, email: str, business: str | None = None, *, identity: str | None = None):
    response = client.post("/api/auth/register", json={
        "full_name": email.split("@")[0].title(),
        "identity_number": identity or _identity_for(email),
        "email": email,
        "password": "StrongPassword123!",
        "confirm_password": "StrongPassword123!",
        "phone": "0771234567",
        "business_name": business or f"{email.split('@')[0]} store",
        "business_type": "Retail",
    })
    assert response.status_code == 200, response.text
    client.csrf = response.json()["csrf_token"]
    return response.json()


def headers(client: TestClient):
    return {"X-CSRF-Token": client.csrf}


def admin_login(client: TestClient):
    response = client.post("/api/auth/login", json={"identifier": settings.admin_email, "password": settings.admin_password})
    assert response.status_code == 200, response.text
    client.csrf = response.json()["csrf_token"]
    return headers(client)


def configure_bank(client: TestClient, h: dict):
    response = client.put("/api/admin/settings/payment", headers=h, json={
        "bank_name": "Commercial Bank", "account_name": "BizFlow LK", "account_number": "0123456789",
        "branch": "Colombo 01", "instructions": "Use your account email as the transfer reference.",
        "currency": "LKR", "whatsapp_number": "0771234567",
    })
    assert response.status_code == 200, response.text


def submit_receipt(client: TestClient, h: dict, plan="STARTER"):
    receipt = b"%PDF-1.4\nBizFlow receipt placeholder for a test.\n%%EOF"
    response = client.post("/api/billing/payment-request", headers=h, data={"plan_id": plan},
                           files={"receipt": ("transfer.pdf", receipt, "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_embedded_preview_auth_cookies_support_login_and_signup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'preview-auth.sqlite3'}")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "private")
    monkeypatch.setattr(settings, "app_env", "test")
    with TestClient(app, base_url="https://8000-preview-check.e2b.app") as preview:
        response = preview.post("/api/auth/register", json={
            "full_name": "Preview Tester", "identity_number": "900000001V", "phone": "0771234567",
            "email": "preview-auth@example.test", "password": "StrongPassword123!",
            "confirm_password": "StrongPassword123!", "business_name": "Preview Test Shop", "business_type": "Retail",
        })
        assert response.status_code == 200, response.text
        cookie = response.headers.get("set-cookie", "").lower()
        assert "samesite=none" in cookie and "secure" in cookie and "httponly" in cookie
        assert preview.get("/api/auth/me").status_code == 200
        csrf = response.json()["csrf_token"]
        assert preview.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}, json={}).status_code == 200
        login = preview.post("/api/auth/login", json={"identifier": "preview-auth@example.test", "password": "StrongPassword123!"})
        assert login.status_code == 200, login.text
        assert preview.get("/api/auth/me").status_code == 200


def test_registration_requires_identity_and_phone_and_creates_unique_free_account(client):
    base = {
        "full_name": "Example Customer", "email": "required@example.test", "password": "StrongPassword123!",
        "confirm_password": "StrongPassword123!", "phone": "0771234567", "business_name": "Required Shop",
        "business_type": "Retail",
    }
    missing_identity = client.post("/api/auth/register", json=base)
    assert missing_identity.status_code == 422
    assert "Identity Number is required" in missing_identity.json()["detail"]
    invalid_identity = client.post("/api/auth/register", json={**base, "identity_number": "not-a-valid-id"})
    assert invalid_identity.status_code == 422
    missing_phone = client.post("/api/auth/register", json={**base, "identity_number": "900111222V", "phone": ""})
    assert missing_phone.status_code == 422

    identity = "900-111-222v"
    normalized_identity = "900111222V"
    created = client.post("/api/auth/register", json={**base, "identity_number": identity})
    assert created.status_code == 200, created.text
    data = created.json()
    assert data["username"] == "BIZ-10001"
    assert data["public_account_id"] == "USR-10001"
    assert data["email"] == base["email"]
    assert identity not in created.text
    client.csrf = data["csrf_token"]

    me = client.get("/api/auth/me").json()
    assert me["current_plan"] == "FREE"
    assert me["subscription"]["status"] == "FREE"
    assert me["membership_role"] == "OWNER"
    assert "identity_number" not in me["user"]
    account = client.get("/api/account").json()["user"]
    assert account["identity_number_masked"] == "••••••••" + normalized_identity[-4:]
    assert "identity_number" not in account

    db = connect()
    row = db.execute("SELECT id,username,public_account_id,identity_number_encrypted,identity_number_lookup_hash,password_hash FROM users WHERE lower(email)=lower(?)", (base["email"],)).fetchone()
    assert row["username"] == data["username"] and row["public_account_id"] == data["public_account_id"]
    assert row["identity_number_encrypted"] != identity
    assert identity not in row["identity_number_encrypted"]
    assert decrypt_identity_number(row["identity_number_encrypted"]) == normalized_identity
    assert row["identity_number_lookup_hash"] != identity
    assert row["password_hash"].startswith("$argon2id$") and "StrongPassword123!" not in row["password_hash"]
    assert db.execute("SELECT COUNT(*) AS n FROM business_members WHERE user_id=? AND role='OWNER'", (row["id"],)).fetchone()["n"] == 1
    db.close()

    duplicate_identity = client.post("/api/auth/register", headers=headers(client), json={**base, "email": "different@example.test", "identity_number": identity})
    assert duplicate_identity.status_code == 409
    assert identity not in duplicate_identity.text and base["email"] not in duplicate_identity.text
    duplicate_email = client.post("/api/auth/register", headers=headers(client), json={**base, "email": base["email"].upper(), "identity_number": "900333444V"})
    assert duplicate_email.status_code == 409
    assert "already exists" in duplicate_email.json()["detail"]
    db = connect()
    assert db.execute("SELECT COUNT(*) AS n FROM users WHERE role='CUSTOMER'").fetchone()["n"] == 1
    db.close()


def test_identity_format_is_configurable(client, monkeypatch):
    monkeypatch.setattr(settings, "identity_number_regex", r"[A-Z]{3}[0-9]{4}")
    account = register(client, "custom-id@example.test", "Custom ID Shop", identity="abc1234")
    masked = client.get("/api/account").json()["user"]["identity_number_masked"]
    assert masked == "••••••••1234"
    assert account["public_account_id"].startswith("USR-")


def test_username_and_email_login_and_generic_password_error(client):
    account = register(client, "login-options@example.test", "Login shop")
    by_username = client.post("/api/auth/login", headers=headers(client), json={"identifier": account["username"], "password": "StrongPassword123!"})
    assert by_username.status_code == 200, by_username.text
    assert by_username.json()["username"] == account["username"]
    client.csrf = by_username.json()["csrf_token"]
    by_email = client.post("/api/auth/login", headers=headers(client), json={"identifier": account["email"], "password": "StrongPassword123!"})
    assert by_email.status_code == 200, by_email.text
    client.csrf = by_email.json()["csrf_token"]
    wrong = client.post("/api/auth/login", headers=headers(client), json={"identifier": account["email"], "password": "WrongPassword123!"})
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "Username/email or password is incorrect."
    assert client.post("/api/auth/forgot-password", headers=headers(client), json={"email": account["email"]}).status_code in {404, 405}
    assert client.post("/api/auth/reset-password", headers=headers(client), json={"token": "unused"}).status_code in {404, 405}


def test_identity_search_and_detail_are_admin_only_and_masked_in_lists(client):
    identity = "900555666V"
    register(client, "privacy@example.test", "Privacy shop", identity=identity)
    customer_id = client.get("/api/auth/me").json()["user"]["id"]
    user_api = client.get("/api/account").json()
    assert identity not in str(user_api)
    assert identity not in client.get("/api/auth/me").text
    denied = client.get(f"/api/admin/users/{customer_id}")
    assert denied.status_code == 403

    admin = TestClient(app)
    admin_headers = admin_login(admin)
    search = admin.post("/api/admin/users/search", headers=admin_headers, json={"search": identity})
    assert search.status_code == 200, search.text
    matches = [row for row in search.json()["items"] if row["id"] == customer_id]
    assert len(matches) == 1
    assert matches[0]["identity_number_masked"] == "••••••••" + identity[-4:]
    assert identity not in search.text
    assert "identity_number_encrypted" not in search.text
    assert "identity_number_lookup_hash" not in search.text

    detail_response = admin.get(f"/api/admin/users/{customer_id}")
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["user"]
    assert detail["identity_number"] == identity
    assert detail["identity_number_masked"] == "••••••••" + identity[-4:]
    assert "identity_number_encrypted" not in detail and "identity_number_lookup_hash" not in detail
    assert "password_hash" not in detail
    db = connect()
    audit = db.execute("SELECT action,description FROM admin_audit_logs WHERE target_user_id=? AND action='ADMIN_VIEW_IDENTITY_NUMBER'", (customer_id,)).fetchall()
    assert audit and identity not in str([dict(x) for x in audit])
    db.close()
    admin.close()


def test_admin_created_customer_starts_free_and_gets_generated_account_credentials(client):
    admin = TestClient(app)
    admin_headers = admin_login(admin)
    payload = {
        "full_name": "Admin Created", "identity_number": "900777888V", "email": "admin-created@example.test",
        "phone": "0777778888", "business_name": "Admin Created Shop", "business_type": "Retail",
    }
    created = admin.post("/api/admin/users/create", headers=admin_headers, json=payload)
    assert created.status_code == 200, created.text
    result = created.json()
    assert result["username"] == "BIZ-10001"
    assert result["public_account_id"] == "USR-10001"
    assert payload["identity_number"] not in created.text
    db = connect()
    user = db.execute("SELECT id,password_hash,must_change_password FROM users WHERE id=?", (result["user_id"],)).fetchone()
    assert user["must_change_password"] == 1
    assert verify_password(result["temporary_password"], user["password_hash"])
    assert db.execute("SELECT status FROM subscriptions WHERE business_id=?", (result["business_id"],)).fetchone()["status"] == "FREE"
    assert db.execute("SELECT role FROM business_members WHERE user_id=?", (result["user_id"],)).fetchone()["role"] == "OWNER"
    db.close()
    paid = admin.post("/api/admin/users/create", headers=admin_headers, json={
        **payload, "email": "admin-created-paid@example.test", "identity_number": "900777889V", "initial_plan": "STARTER",
    })
    assert paid.status_code == 422
    admin.close()


def test_admin_temporary_password_revokes_sessions_forces_change_and_audits_without_secrets(client):
    account = register(client, "temporary@example.test", "Temporary shop")
    customer_id = client.get("/api/auth/me").json()["user"]["id"]
    second = TestClient(app)
    second_login = second.post("/api/auth/login", json={"identifier": account["email"], "password": "StrongPassword123!"})
    assert second_login.status_code == 200
    second.csrf = second_login.json()["csrf_token"]

    admin = TestClient(app)
    admin_headers = admin_login(admin)
    reset = admin.post(f"/api/admin/users/{customer_id}/temporary-password", headers=admin_headers, json={})
    assert reset.status_code == 200, reset.text
    temporary = reset.json()["temporary_password"]
    assert len(temporary) >= 12
    assert "password_hash" not in reset.text and "identity_number" not in reset.text
    assert client.get("/api/auth/me").status_code == 401
    assert second.get("/api/auth/me").status_code == 401

    db = connect()
    row = db.execute("SELECT password_hash,must_change_password FROM users WHERE id=?", (customer_id,)).fetchone()
    assert row["must_change_password"] == 1
    assert temporary != row["password_hash"] and verify_password(temporary, row["password_hash"])
    audit = db.execute("SELECT action,description FROM admin_audit_logs WHERE target_user_id=? ORDER BY created_at DESC LIMIT 1", (customer_id,)).fetchone()
    assert audit["action"] == "ADMIN_PASSWORD_RESET"
    assert temporary not in audit["description"] and row["password_hash"] not in audit["description"]
    assert db.execute("SELECT COUNT(*) AS n FROM sessions WHERE user_id=?", (customer_id,)).fetchone()["n"] == 0
    db.close()

    old_password = client.post("/api/auth/login", json={"identifier": account["email"], "password": "StrongPassword123!"})
    assert old_password.status_code == 401
    login = client.post("/api/auth/login", json={"identifier": account["username"], "password": temporary})
    assert login.status_code == 200 and login.json()["must_change_password"] is True
    client.csrf = login.json()["csrf_token"]
    blocked = client.get("/api/products")
    assert blocked.status_code == 403
    assert blocked.headers.get("X-BizFlow-Error") == "PASSWORD_CHANGE_REQUIRED"
    blocked_receipt = client.get("/api/receipts/not-existing")
    assert blocked_receipt.status_code == 403
    assert blocked_receipt.headers.get("X-BizFlow-Error") == "PASSWORD_CHANGE_REQUIRED"
    changed = client.post("/api/auth/change-password", headers=headers(client), json={
        "current_password": temporary, "new_password": "NewStrongPassword456!", "confirm_password": "NewStrongPassword456!",
    })
    assert changed.status_code == 200, changed.text
    client.csrf = changed.json()["csrf_token"]
    assert client.get("/api/auth/me").json()["must_change_password"] is False
    assert client.get("/api/receipts/not-existing").status_code == 404
    db = connect()
    assert db.execute("SELECT COUNT(*) AS n FROM sessions WHERE user_id=?", (customer_id,)).fetchone()["n"] == 1
    latest_audit = db.execute("SELECT action,description FROM admin_audit_logs WHERE target_user_id=? AND action='ADMIN_PASSWORD_RESET'", (customer_id,)).fetchone()
    assert temporary not in latest_audit["description"]
    db.close()
    assert client.post("/api/auth/login", headers=headers(client), json={"identifier": account["email"], "password": "NewStrongPassword456!"}).status_code == 200
    assert (Path(__file__).resolve().parents[1] / "web/assets/pages.js").read_text().find("/settings/security") >= 0
    second.close()
    admin.close()


def test_support_defaults_and_account_recovery_message_has_no_identity_number(client):
    public = client.get("/api/public/settings").json()
    assert public["support_phone"] == ""
    assert public["support_whatsapp"] == ""
    assert public["whatsapp_number"] == ""
    assert public["support_message"]
    public_js = (Path(__file__).resolve().parents[1] / "web/assets/public.js").read_text()
    app_js = (Path(__file__).resolve().parents[1] / "web/assets/app.js").read_text()
    recovery = public_js[public_js.index("if (path === '/forgot-password')"):public_js.index('data-form="login"')]
    assert 'href="tel:${esc(phoneHref)}"' in recovery
    assert 'data-action="recovery-whatsapp"' in recovery
    assert 'name="identity_number"' not in recovery
    for field in ("Account ID", "Username", "Name", "Email", "Phone"):
        assert f"{field}: ${{data." in app_js

    admin = TestClient(app)
    admin_headers = admin_login(admin)
    saved = admin.put("/api/admin/settings/general", headers=admin_headers, json={
        "website_name": "BizFlow LK", "business_name": "BizFlow LK", "support_email": "help@example.test",
        "support_phone": "0771234567", "support_whatsapp": "0771234567", "support_message": "Please send your account references.",
    })
    assert saved.status_code == 200, saved.text
    updated = admin.get("/api/public/settings").json()
    assert updated["support_phone"] == "0771234567"
    assert updated["support_whatsapp"] == "0771234567"
    assert updated["support_message"] == "Please send your account references."
    assert updated["support_email"] == "help@example.test"
    admin.close()


def test_argon2id_password_storage_and_legacy_scrypt_upgrade():
    password = "Legacy-Password-For-Test-2026!"
    salt = b"\x42" * 16
    derived = hashlib.scrypt(password.encode(), salt=salt, n=1 << 14, r=8, p=1, dklen=64)
    legacy_hash = f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"
    assert verify_password(password, legacy_hash)
    assert password_needs_rehash(legacy_hash)
    encoded = hash_password(password)
    assert encoded.startswith("$argon2id$")
    assert not password_needs_rehash(encoded)
    assert verify_password(password, encoded)
    assert not verify_password("not-the-password", encoded)
    assert password not in encoded


def test_database_rate_limits_and_request_ids(client):
    for _ in range(8):
        response = client.post("/api/auth/login", json={"identifier": "limited@example.test", "password": "wrong"})
        assert response.status_code == 401
    limited = client.post("/api/auth/login", json={"identifier": "limited@example.test", "password": "wrong"})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    too_large = client.post("/api/auth/login", content=b"x" * (8 * 1024 * 1024),
                            headers={"Content-Type": "application/json"})
    assert too_large.status_code == 413
    assert len(too_large.headers.get("x-request-id", "")) == 32
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.headers.get("cache-control") == "no-store"
    assert health.headers.get("x-content-type-options") == "nosniff"
    assert len(health.headers.get("x-request-id", "")) == 32
    db = connect()
    try:
        versions = {row["version"] for row in db.execute("SELECT version FROM schema_migrations").fetchall()}
        assert versions == {"0001_initial_schema", "0002_rate_limits_and_payment_guard"}
    finally:
        db.close()


def test_one_pending_payment_request_per_business(client):
    register(client, "pending-guard@example.test", "Pending Guard Shop")
    customer_headers = headers(client)
    admin = TestClient(app)
    admin_headers = admin_login(admin)
    configure_bank(admin, admin_headers)
    first = submit_receipt(client, customer_headers)
    receipt = b"%PDF-1.4\nTest receipt\n%%EOF"
    duplicate = client.post("/api/billing/payment-request", headers=customer_headers,
                            data={"plan_id": "STARTER"},
                            files={"receipt": ("transfer.pdf", receipt, "application/pdf")})
    assert duplicate.status_code == 409
    db = connect()
    try:
        assert db.execute("SELECT COUNT(*) AS n FROM payment_requests WHERE business_id=(SELECT business_id FROM payment_requests WHERE id=?) AND status='PENDING'", (first,)).fetchone()["n"] == 1
    finally:
        db.close()
    admin.close()


def test_registration_is_free_and_csrf_protected(client):
    register(client, "free@example.test")
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["current_plan"] == "FREE"
    assert me.json()["subscription"]["status"] == "FREE"
    product = client.post("/api/products", json={"name": "No CSRF", "selling_price": 100})
    assert product.status_code == 403
    assert client.get("/api/dashboard").status_code == 200


def test_business_data_isolation_and_sale_stock_transaction(client):
    first = TestClient(app)
    second = TestClient(app)
    register(first, "one@example.test", "One Shop")
    register(second, "two@example.test", "Two Shop")
    h1, h2 = headers(first), headers(second)
    product = first.post("/api/products", headers=h1, json={
        "name": "Tea", "purchase_price": 200, "selling_price": 450, "quantity": 3, "minimum_stock": 1,
    })
    assert product.status_code == 200, product.text
    product_id = product.json()["id"]
    assert second.get("/api/products").json()["items"] == []
    assert second.put(f"/api/products/{product_id}", headers=h2, json={"name": "Stolen"}).status_code == 404
    failed_sale = first.post("/api/sales", headers=h1, json={"items": [{"product_id": product_id, "quantity": 4}]})
    assert failed_sale.status_code == 409
    inventory = first.get("/api/inventory").json()
    assert inventory["items"][0]["quantity"] == 3
    sale = first.post("/api/sales", headers=h1, json={"items": [{"product_id": product_id, "quantity": 2}], "payment_method": "Cash"})
    assert sale.status_code == 200, sale.text
    assert first.get("/api/inventory").json()["items"][0]["quantity"] == 1
    assert len(second.get("/api/sales").json()["items"]) == 0
    first.close()
    second.close()


def test_manual_approval_is_required_and_duplicate_approval_is_blocked(client):
    admin = TestClient(app)
    admin_headers = admin_login(admin)
    configure_bank(admin, admin_headers)
    register(client, "payer@example.test", "Payer store")
    customer_headers = headers(client)
    request_id = submit_receipt(client, customer_headers)
    assert client.get("/api/billing").json()["current_plan"] == "FREE"
    customer_receipt = client.get(f"/api/receipts/{request_id}")
    assert customer_receipt.status_code == 200
    other = TestClient(app)
    register(other, "other@example.test", "Other store")
    assert other.get(f"/api/receipts/{request_id}").status_code == 403
    missing_verification = admin.post(f"/api/admin/payment-requests/{request_id}/approve", headers=admin_headers, json={})
    assert missing_verification.status_code == 422
    approval = admin.post(f"/api/admin/payment-requests/{request_id}/approve", headers=admin_headers,
                          json={"verified": True, "admin_note": "Transfer independently confirmed."})
    assert approval.status_code == 200, approval.text
    details = client.get("/api/billing").json()
    assert details["current_plan"] == "STARTER"
    assert details["subscription"]["status"] == "ACTIVE"
    assert parse_dt(details["subscription"]["expiry_date"]) - parse_dt(details["subscription"]["start_date"]) == timedelta(days=30)
    product = client.post("/api/products", headers=customer_headers,
                          json={"name": "Approved-plan product", "purchase_price": 120, "selling_price": 250, "quantity": 5})
    assert product.status_code == 200, product.text
    product_id = product.json()["id"]
    quote = client.post("/api/quotations", headers=customer_headers,
                        json={"items": [{"product_id": product_id, "quantity": 2}], "valid_until": "2026-12-01"})
    assert quote.status_code == 200, quote.text
    quote_id = quote.json()["id"]
    revised = client.put(f"/api/quotations/{quote_id}", headers=customer_headers,
                         json={"items": [{"product_id": product_id, "quantity": 1, "unit_price": 300}], "valid_until": "2026-12-15"})
    assert revised.status_code == 200, revised.text
    assert client.post(f"/api/quotations/{quote_id}/status", headers=customer_headers, json={"status": "ACCEPTED"}).status_code == 200
    converted = client.post(f"/api/quotations/{quote_id}/convert", headers=customer_headers, json={})
    assert converted.status_code == 200, converted.text
    assert client.get("/api/features/pdf_invoices", headers=customer_headers).status_code == 200
    expense = client.post("/api/expenses", headers=customer_headers,
                          json={"category": "Utilities", "amount": 1500, "expense_date": "2026-10-04"})
    assert expense.status_code == 200, expense.text
    expense_receipt = client.post(f"/api/expenses/{expense.json()['id']}/receipt", headers=customer_headers,
                                  files={"receipt": ("utility.pdf", b"%PDF-1.4\nreceipt\n%%EOF", "application/pdf")})
    assert expense_receipt.status_code == 200, expense_receipt.text
    assert client.get(expense_receipt.json()["receipt_url"]).status_code == 200
    assert other.get(expense_receipt.json()["receipt_url"]).status_code == 402
    duplicate = admin.post(f"/api/admin/payment-requests/{request_id}/approve", headers=admin_headers,
                           json={"verified": True})
    assert duplicate.status_code == 409
    assert "already been approved" in duplicate.json()["detail"]
    admin.close()
    other.close()


def test_early_renewal_adds_thirty_days_and_expired_renewal_starts_fresh(client, tmp_path):
    admin = TestClient(app)
    ah = admin_login(admin)
    configure_bank(admin, ah)
    register(client, "renew@example.test", "Renew shop")
    ch = headers(client)
    first_id = submit_receipt(client, ch)
    assert admin.post(f"/api/admin/payment-requests/{first_id}/approve", headers=ah, json={"verified": True}).status_code == 200
    first = client.get("/api/billing").json()["subscription"]
    first_start = parse_dt(first["start_date"])
    first_expiry = parse_dt(first["expiry_date"])
    second_id = submit_receipt(client, ch)
    response = admin.post(f"/api/admin/payment-requests/{second_id}/approve", headers=ah, json={"verified": True})
    assert response.status_code == 200, response.text
    early = client.get("/api/billing").json()["subscription"]
    assert parse_dt(early["start_date"]) == first_start
    assert parse_dt(early["expiry_date"]) - first_expiry == timedelta(days=30)

    db = connect()
    past = iso_utc(utc_now() - timedelta(seconds=2))
    with transaction(db):
        db.execute("UPDATE subscriptions SET expiry_date=?,status='ACTIVE',plan_id='STARTER' WHERE business_id=?",
                   (past, early["business_id"]))
    db.close()
    assert client.get("/api/billing").json()["current_plan"] == "FREE"
    third_id = submit_receipt(client, ch)
    before = utc_now()
    response = admin.post(f"/api/admin/payment-requests/{third_id}/approve", headers=ah, json={"verified": True})
    assert response.status_code == 200, response.text
    renewed = client.get("/api/billing").json()["subscription"]
    new_start = parse_dt(renewed["start_date"])
    new_expiry = parse_dt(renewed["expiry_date"])
    assert abs((new_start - before).total_seconds()) < 5
    assert new_expiry - new_start == timedelta(days=30)
    admin.close()


def test_expiration_is_idempotent_preserves_business_data_and_locks_paid_features(client):
    register(client, "expire@example.test", "Expire shop")
    h = headers(client)
    product = client.post("/api/products", headers=h, json={"name": "Saved record", "purchase_price": 25, "selling_price": 40, "quantity": 4})
    assert product.status_code == 200
    me = client.get("/api/auth/me").json()
    db = connect()
    now = iso_utc()
    with transaction(db):
        db.execute("UPDATE subscriptions SET plan_id='STARTER',status='ACTIVE',start_date=?,expiry_date=? WHERE business_id=?",
                   (iso_utc(utc_now()-timedelta(days=30)), iso_utc(utc_now()-timedelta(microseconds=1)), me["business"]["business_id"]))
    run_billing_jobs(db)
    run_billing_jobs(db)
    events = db.execute("SELECT COUNT(*) AS n FROM subscription_events WHERE business_id=? AND event_type='EXPIRED'", (me["business"]["business_id"],)).fetchone()["n"]
    assert events == 1
    db.close()
    assert client.get("/api/auth/me").json()["current_plan"] == "FREE"
    locked = client.get("/api/suppliers")
    assert locked.status_code == 402
    assert client.get("/api/products").json()["items"][0]["name"] == "Saved record"


def test_product_images_are_validated_and_private_and_pdf_feature_is_server_locked(client):
    register(client, "images@example.test", "Image shop")
    h = headers(client)
    product = client.post("/api/products", headers=h, json={"name": "Coffee", "selling_price": 450, "quantity": 2})
    assert product.status_code == 200, product.text
    product_id = product.json()["id"]
    assert client.get("/api/features/pdf_invoices").status_code == 402
    image_stream = io.BytesIO()
    Image.new("RGB", (1, 1), (24, 64, 112)).save(image_stream, format="PNG")
    image = image_stream.getvalue()
    upload = client.post(f"/api/products/{product_id}/image", headers=h,
                         files={"image": ("coffee.png", image, "image/png")})
    assert upload.status_code == 200, upload.text
    viewed = client.get(upload.json()["image_url"])
    assert viewed.status_code == 200
    assert viewed.content == image
    assert "private" in viewed.headers["cache-control"]
    db = connect()
    try:
        saved = db.execute("SELECT image_path FROM products WHERE id=?", (product_id,)).fetchone()
    finally:
        db.close()
    assert stat.S_IMODE(Path(saved["image_path"]).stat().st_mode) == 0o600
    assert stat.S_IMODE((Path(saved["image_path"]).parent).stat().st_mode) == 0o700
    mismatch = client.post(f"/api/products/{product_id}/image", headers=h,
                           files={"image": ("coffee.png", image, "image/jpeg")})
    assert mismatch.status_code == 415
    other = TestClient(app)
    register(other, "image-other@example.test", "Other image shop")
    assert other.get(f"/api/products/{product_id}/image").status_code == 404
    other.close()


def test_rejection_does_not_activate_subscription_and_admin_routes_are_protected(client):
    register(client, "reject@example.test", "Reject shop")
    assert client.get("/api/admin/dashboard").status_code == 403
    admin = TestClient(app)
    ah = admin_login(admin)
    configure_bank(admin, ah)
    request_id = submit_receipt(client, headers(client))
    rejected = admin.post(f"/api/admin/payment-requests/{request_id}/reject", headers=ah,
                          json={"admin_note": "Transfer amount does not match the selected plan."})
    assert rejected.status_code == 200, rejected.text
    assert client.get("/api/billing").json()["current_plan"] == "FREE"
    approval = admin.post(f"/api/admin/payment-requests/{request_id}/approve", headers=ah, json={"verified": True})
    assert approval.status_code == 409
    admin.close()


def test_suspension_blocks_business_operations_without_deleting_data(client):
    admin = TestClient(app)
    ah = admin_login(admin)
    register(client, "suspend@example.test", "Suspend shop")
    h = headers(client)
    client.post("/api/products", headers=h, json={"name": "Keep me", "selling_price": 100, "quantity": 1})
    user = client.get("/api/auth/me").json()["user"]
    response = admin.post(f"/api/admin/users/{user['id']}/suspend", headers=ah, json={"reason": "Test suspension"})
    assert response.status_code == 200
    assert client.get("/api/products").status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    db = connect()
    try:
        assert db.execute("SELECT COUNT(*) AS n FROM products WHERE name='Keep me'").fetchone()["n"] == 1
    finally:
        db.close()
    admin.close()
