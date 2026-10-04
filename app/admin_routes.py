from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from app.config import settings
from app.account_security import (decrypt_identity_number, encrypt_identity_number, generate_account_identifiers,
                                  generate_temporary_password, identity_lookup_hash, mask_identity_number,
                                  normalize_identity_number)
from app.db import get_db, transaction
from app.deps import get_admin_user, require_admin_permission
from app.rate_limits import enforce_rate_limit
from app.security import (clean_email, clean_money, clean_phone, clean_text, hash_password, iso_utc,
                          make_id, parse_dt, utc_now)
from app.services import (add_audit, add_notification, create_business_for_user,
                          expire_one_in_transaction, run_billing_jobs)
from app.timeutils import LOCAL_TZ, local_today, utc_bounds

router = APIRouter(prefix="/api/admin", tags=["administration"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _admin_run_jobs(db: Any) -> None:
    run_billing_jobs(db)


def _plan(db: Any, plan_id: str) -> Any:
    plan = db.execute("SELECT * FROM plans WHERE id=?", (plan_id.upper(),)).fetchone()
    if not plan or not plan["active"]:
        raise HTTPException(status_code=404, detail="This plan is not available.")
    return plan


def _user_business(db: Any, user_id: str) -> Any:
    return db.execute("SELECT b.* FROM businesses b JOIN business_members bm ON bm.business_id=b.id WHERE bm.user_id=? ORDER BY CASE WHEN bm.role='OWNER' THEN 0 ELSE 1 END,bm.joined_at LIMIT 1",
                      (user_id,)).fetchone()


def _ensure_subscription(db: Any, business_id: str, now: str) -> Any:
    lock_suffix = " FOR UPDATE" if getattr(db, "dialect", "sqlite") == "postgres" else ""
    sub = db.execute(f"SELECT * FROM subscriptions WHERE business_id=?{lock_suffix}", (business_id,)).fetchone()
    if not sub:
        sub_id = make_id()
        db.execute("INSERT INTO subscriptions(id,business_id,plan_id,status,start_date,expiry_date,created_at,updated_at) VALUES(?,?,'FREE','FREE',NULL,NULL,?,?)",
                   (sub_id, business_id, now, now))
        sub = db.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    return sub


def _timestamp(value: str, *, field: str = "Expiry date") -> datetime:
    try:
        if len(value) == 10:
            d = datetime.fromisoformat(value).date()
            parsed = datetime.combine(d + timedelta(days=1), datetime.min.time(), LOCAL_TZ).astimezone(timezone.utc)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TZ)
            parsed = parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"Enter a valid {field.lower()}.") from None
    return parsed


def _activate_in_transaction(db: Any, *, business_id: str, plan: Any, actor_id: str,
                             duration_days: int, request_id: str | None, event_type: str,
                             now_dt: datetime, custom_expiry: datetime | None = None,
                             details: dict | None = None) -> dict:
    now = iso_utc(now_dt)
    expire_one_in_transaction(db, business_id, now_dt=now_dt)
    sub = _ensure_subscription(db, business_id, now)
    old = dict(sub)
    old_expiry = parse_dt(sub["expiry_date"])
    continuing = sub["status"] == "ACTIVE" and old_expiry is not None and old_expiry > now_dt
    if continuing:
        start = parse_dt(sub["start_date"]) or now_dt
        base = old_expiry
    else:
        start = now_dt
        base = now_dt
    expiry = custom_expiry or (base + timedelta(days=duration_days))
    if expiry <= now_dt:
        raise HTTPException(status_code=422, detail="Subscription expiry must be in the future.")
    start_value, expiry_value = iso_utc(start), iso_utc(expiry)
    db.execute("UPDATE subscriptions SET plan_id=?,status='ACTIVE',start_date=?,expiry_date=?,updated_at=? WHERE id=?",
               (plan["id"], start_value, expiry_value, now, sub["id"]))
    event_key = f"{event_type.lower()}:{request_id}" if request_id else f"{event_type.lower()}:{make_id()}"
    db.execute(
        "INSERT INTO subscription_events(id,idempotency_key,business_id,subscription_id,event_type,old_plan_id,new_plan_id,old_status,new_status,old_start_date,old_expiry_date,new_start_date,new_expiry_date,actor_user_id,payment_request_id,details,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING",
        (make_id(), event_key, business_id, sub["id"], event_type, old["plan_id"], plan["id"], old["status"], "ACTIVE",
         old["start_date"], old["expiry_date"], start_value, expiry_value, actor_id, request_id,
         json.dumps(details or {"duration_days": duration_days, "renewal_preserved_remaining_days": continuing}), now),
    )
    return {"subscription_id": sub["id"], "start_date": start_value, "expiry_date": expiry_value,
            "old_plan_id": old["plan_id"], "old_status": old["status"], "old_expiry_date": old["expiry_date"],
            "continued": continuing}


def _user_summary_query() -> str:
    return (
        "SELECT u.id,u.public_account_id,u.full_name,u.username,u.email,u.phone,u.identity_number_encrypted,"
        "u.account_status,u.created_at,COALESCE(u.last_login_at,u.last_login) AS last_login,u.must_change_password,"
        "(u.password_hash IS NOT NULL) AS has_password,"
        "b.id AS business_id,b.name AS business_name,b.business_type,bm.role AS membership_role,"
        "s.status AS subscription_status,s.start_date AS subscription_start,s.expiry_date AS subscription_expiry,s.plan_id AS stored_plan,"
        "CASE WHEN s.status='ACTIVE' AND s.expiry_date>? THEN s.plan_id ELSE 'FREE' END AS current_plan "
        "FROM users u LEFT JOIN business_members bm ON bm.user_id=u.id "
        "LEFT JOIN businesses b ON b.id=bm.business_id LEFT JOIN subscriptions s ON s.business_id=b.id WHERE u.role='CUSTOMER'"
    )


@router.get("/dashboard")
def admin_dashboard(admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    _admin_run_jobs(db)
    now = iso_utc()
    month = local_today().replace(day=1)
    month_start, month_end = utc_bounds(month, local_today() + timedelta(days=1))
    total_users = db.execute("SELECT COUNT(*) AS n FROM users WHERE role='CUSTOMER'").fetchone()["n"]
    total_businesses = db.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()["n"]
    free_count = db.execute("SELECT COUNT(*) AS n FROM subscriptions WHERE status IN ('FREE','EXPIRED','CANCELLED')").fetchone()["n"]
    active_count = db.execute("SELECT COUNT(*) AS n FROM subscriptions WHERE status='ACTIVE' AND expiry_date>?", (now,)).fetchone()["n"]
    expired_count = db.execute("SELECT COUNT(*) AS n FROM subscriptions WHERE status='EXPIRED'").fetchone()["n"]
    pending_count = db.execute("SELECT COUNT(*) AS n FROM payment_requests WHERE status='PENDING'").fetchone()["n"]
    revenue = db.execute("SELECT COALESCE(SUM(p.amount),0) AS amount FROM payments p JOIN payment_requests pr ON pr.id=p.payment_request_id "
                         "WHERE pr.status='APPROVED' AND p.status='APPROVED' AND p.paid_at>=? AND p.paid_at<?", (month_start, month_end)).fetchone()["amount"]
    reg_start = iso_utc(utc_now() - timedelta(days=30))
    new_registrations = db.execute("SELECT COUNT(*) AS n FROM users WHERE role='CUSTOMER' AND created_at>=?", (reg_start,)).fetchone()["n"]
    recent_requests = db.execute("SELECT pr.id,pr.status,pr.amount,pr.currency,pr.submitted_at,pl.name AS plan_name,u.full_name,u.email,b.name AS business_name "
                                 "FROM payment_requests pr JOIN plans pl ON pl.id=pr.plan_id JOIN users u ON u.id=pr.user_id JOIN businesses b ON b.id=pr.business_id "
                                 "ORDER BY pr.submitted_at DESC LIMIT 8").fetchall()
    recently_expired = db.execute("SELECT se.created_at,se.old_plan_id,b.name AS business_name,u.full_name,u.email FROM subscription_events se "
                                  "JOIN businesses b ON b.id=se.business_id JOIN users u ON u.id=b.owner_user_id WHERE se.event_type='EXPIRED' ORDER BY se.created_at DESC LIMIT 6").fetchall()
    recently_activated = db.execute("SELECT se.created_at,se.new_plan_id,b.name AS business_name,u.full_name,u.email FROM subscription_events se "
                                    "JOIN businesses b ON b.id=se.business_id JOIN users u ON u.id=b.owner_user_id "
                                    "WHERE se.event_type IN ('PAYMENT_APPROVED','MANUAL_ACTIVATION','MANUAL_EXTEND') ORDER BY se.created_at DESC LIMIT 6").fetchall()
    registrations = db.execute("SELECT full_name,email,created_at FROM users WHERE role='CUSTOMER' ORDER BY created_at DESC LIMIT 6").fetchall()
    return {
        "cards": {"total_users": total_users, "total_businesses": total_businesses, "free_businesses": free_count,
                  "active_paid_businesses": active_count, "expired_businesses": expired_count,
                  "pending_payments": pending_count, "monthly_revenue": revenue},
        "new_registrations_30d": new_registrations,
        "recent_payment_requests": [dict(r) for r in recent_requests],
        "recently_expired": [dict(r) for r in recently_expired],
        "recently_activated": [dict(r) for r in recently_activated],
        "newest_registrations": [dict(r) for r in registrations],
    }


def _user_search_results(db: Any, *, search: str, plan: str, account_status: str,
                         subscription_status: str, identity_hash: str | None = None) -> dict:
    _admin_run_jobs(db)
    sql = _user_summary_query()
    params: list[Any] = [iso_utc()]
    term = search.strip()[:150]
    if term:
        sql += " AND (lower(u.full_name) LIKE lower(?) OR lower(u.username) LIKE lower(?) OR lower(u.public_account_id) LIKE lower(?) OR lower(u.email) LIKE lower(?) OR lower(COALESCE(b.name,'')) LIKE lower(?) OR lower(COALESCE(u.phone,'')) LIKE lower(?)"
        like = f"%{term}%"
        params.extend([like, like, like, like, like, like])
        if identity_hash:
            sql += " OR u.identity_number_lookup_hash=?"
            params.append(identity_hash)
        sql += ")"
    if plan:
        sql += " AND (CASE WHEN s.status='ACTIVE' AND s.expiry_date>? THEN s.plan_id ELSE 'FREE' END)=?"
        params.extend([iso_utc(), plan.upper()])
    if account_status:
        sql += " AND u.account_status=?"
        params.append(account_status.upper())
    if subscription_status:
        sql += " AND s.status=?"
        params.append(subscription_status.upper())
    sql += " ORDER BY u.created_at DESC LIMIT 1000"
    rows = db.execute(sql, tuple(params)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        encrypted = item.pop("identity_number_encrypted", None)
        item["identity_number_masked"] = mask_identity_number(decrypt_identity_number(encrypted))
        item["auth_methods"] = (["Username/email and password"] if item.pop("has_password", False) else [])
        items.append(item)
    return {"items": items}


@router.get("/users")
def list_users(
    search: str = "", plan: str = "", account_status: str = "", subscription_status: str = "",
    admin: dict = Depends(get_admin_user), db=Depends(get_db),
):
    # This legacy GET search intentionally searches only non-sensitive fields. Identity numbers
    # are accepted only in the POST search body so they never land in a URL or access log.
    return _user_search_results(db, search=search, plan=plan, account_status=account_status,
                                subscription_status=subscription_status)


@router.post("/users/search")
def search_users(payload: dict = Body(...), admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    try:
        term = clean_text(payload.get("search", ""), limit=150, label="Search")
        plan = clean_text(payload.get("plan", ""), limit=20, label="Plan")
        account_status = clean_text(payload.get("account_status", ""), limit=20, label="Account status")
        subscription_status = clean_text(payload.get("subscription_status", ""), limit=20, label="Subscription status")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    identity_hash = None
    if term:
        try:
            normalized = normalize_identity_number(term)
        except ValueError:
            normalized = None
        if normalized:
            require_admin_permission(admin, "VIEW_IDENTITY_NUMBER")
            identity_hash = identity_lookup_hash(normalized)
    return _user_search_results(db, search=term, plan=plan, account_status=account_status,
                                subscription_status=subscription_status, identity_hash=identity_hash)


@router.get("/users/{user_id}")
def user_detail(user_id: str, request: Request, admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    require_admin_permission(admin, "VIEW_IDENTITY_NUMBER")
    _admin_run_jobs(db)
    user = db.execute("SELECT id,public_account_id,full_name,username,email,phone,identity_number_encrypted,avatar_url,role,account_status,created_at,COALESCE(last_login_at,last_login) AS last_login,must_change_password,password_changed_at,(password_hash IS NOT NULL) AS has_password FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user["role"] == "SUPER_ADMIN":
        raise HTTPException(status_code=404, detail="Customer not found.")
    user_data = dict(user)
    encrypted = user_data.pop("identity_number_encrypted", None)
    identity = decrypt_identity_number(encrypted)
    user_data["identity_number"] = identity
    user_data["identity_number_masked"] = mask_identity_number(identity)
    user_data["auth_methods"] = (["Username/email and password"] if user_data.pop("has_password", False) else [])
    business = _user_business(db, user_id)
    subscription = None
    requests = []
    events = []
    if business:
        subscription = db.execute("SELECT s.*,p.name AS plan_name,p.price AS plan_price FROM subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.business_id=?", (business["id"],)).fetchone()
        requests = db.execute("SELECT pr.*,p.name AS plan_name FROM payment_requests pr JOIN plans p ON p.id=pr.plan_id WHERE pr.business_id=? ORDER BY pr.submitted_at DESC LIMIT 100", (business["id"],)).fetchall()
        events = db.execute("SELECT * FROM subscription_events WHERE business_id=? ORDER BY created_at DESC LIMIT 50", (business["id"],)).fetchall()
    payment_rows = db.execute("SELECT p.* FROM payments p WHERE p.user_id=? ORDER BY p.paid_at DESC LIMIT 100", (user_id,)).fetchall()
    recent_audit = db.execute("SELECT * FROM admin_audit_logs WHERE target_user_id=? ORDER BY created_at DESC LIMIT 50", (user_id,)).fetchall()
    now = iso_utc()
    with transaction(db):
        add_audit(db, admin_id=admin["id"], target_user_id=user_id,
                  action="ADMIN_VIEW_IDENTITY_NUMBER", description="Administrator viewed a customer's identity number.",
                  ip_address=_ip(request), now=now)
    return {"user": user_data, "business": dict(business) if business else None,
            "subscription": dict(subscription) if subscription else None,
            "payment_requests": [_admin_payment_row(r) for r in requests],
            "payments": [dict(r) for r in payment_rows], "subscription_events": [dict(r) for r in events],
            "audit_history": [dict(r) for r in recent_audit]}


@router.post("/users/{user_id}/temporary-password")
def admin_reset_password(
    user_id: str,
    request: Request,
    admin: dict = Depends(get_admin_user),
    db=Depends(get_db),
):
    user = db.execute("SELECT id,role FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user["role"] != "CUSTOMER":
        raise HTTPException(status_code=404, detail="Customer not found.")
    enforce_rate_limit(request, scope="admin-password-reset", subject=f"{admin['id']}:{user_id}",
                       limit=5, window_seconds=3600)
    temporary_password = generate_temporary_password()
    password_hash = hash_password(temporary_password)
    now = iso_utc()
    with transaction(db):
        db.execute("UPDATE users SET password_hash=?,must_change_password=1,password_changed_at=?,updated_at=? WHERE id=?",
                   (password_hash, now, now, user_id))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        add_audit(db, admin_id=admin["id"], target_user_id=user_id,
                  action="ADMIN_PASSWORD_RESET", description="Administrator issued a one-time temporary password; user must change it at next sign-in.",
                  ip_address=_ip(request), now=now)
    return {"ok": True, "temporary_password": temporary_password,
            "message": "Temporary password generated. Share it securely; it is shown only once."}


def _admin_payment_row(row: Any) -> dict:
    result = dict(row)
    result.pop("receipt_path", None)
    result["receipt_url"] = f"/api/receipts/{result['id']}"
    return result


@router.post("/users/create")
def admin_create_user(request: Request, payload: dict = Body(...),
                      admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    require_admin_permission(admin, "VIEW_IDENTITY_NUMBER")
    enforce_rate_limit(request, scope="admin-create-user", subject=admin["id"], limit=30, window_seconds=3600)
    try:
        name = clean_text(payload.get("full_name"), limit=120, required=True, label="Full Name")
        identity_number = normalize_identity_number(payload.get("identity_number"))
        email = clean_email(payload.get("email"))
        phone = clean_phone(payload.get("phone"))
        business_name = clean_text(payload.get("business_name"), limit=160, required=True, label="Business name")
        business_type = clean_text(payload.get("business_type") or "Other", limit=80, label="Business type")
        requested_plan = clean_text(payload.get("initial_plan") or "FREE", limit=20).upper()
        if requested_plan != "FREE":
            raise ValueError("New accounts start on Free. Paid access requires a submitted transfer receipt and administrator approval.")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    identity_hash = identity_lookup_hash(identity_number)
    if db.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    if db.execute("SELECT id FROM users WHERE identity_number_lookup_hash=?", (identity_hash,)).fetchone():
        raise HTTPException(status_code=409, detail="This identity number is already associated with an account.")
    temporary_password = generate_temporary_password()
    pw_hash = hash_password(temporary_password)
    user_id, now = make_id(), iso_utc()
    business_id = None
    try:
        with transaction(db):
            if db.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
                raise HTTPException(status_code=409, detail="An account with this email already exists.")
            if db.execute("SELECT id FROM users WHERE identity_number_lookup_hash=?", (identity_hash,)).fetchone():
                raise HTTPException(status_code=409, detail="This identity number is already associated with an account.")
            username, public_account_id = generate_account_identifiers(db)
            db.execute(
                "INSERT INTO users(id,public_account_id,full_name,username,identity_number_encrypted,identity_number_lookup_hash,email,phone,password_hash,must_change_password,role,account_status,password_changed_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,1,'CUSTOMER','ACTIVE',?,?,?)",
                (user_id, public_account_id, name, username, encrypt_identity_number(identity_number), identity_hash,
                 email, phone, pw_hash, now, now, now),
            )
            business_id = create_business_for_user(db, user_id, business_name, business_type, phone, email)
            add_audit(db, admin_id=admin["id"], target_user_id=user_id, target_business_id=business_id,
                      action="USER_CREATED", description=f"Admin created a Free customer account for {email}.",
                      ip_address=_ip(request), now=now)
    except Exception as exc:
        if isinstance(exc, sqlite3.IntegrityError) or getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="An account could not be created because an email or identity number is already in use.") from None
        raise
    return {"ok": True, "user_id": user_id, "business_id": business_id, "username": username,
            "public_account_id": public_account_id, "temporary_password": temporary_password,
            "message": "Customer account created on the Free plan. The temporary password must be changed on first sign-in."}


@router.get("/businesses")
def list_businesses(search: str = "", plan: str = "", admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    _admin_run_jobs(db)
    sql = "SELECT b.id,b.name,b.business_type,b.currency,b.created_at,u.id AS owner_user_id,u.full_name AS owner_name,u.email,u.phone,u.account_status,"
    sql += "s.status AS subscription_status,s.start_date AS expiry_start,s.expiry_date,CASE WHEN s.status='ACTIVE' AND s.expiry_date>? THEN s.plan_id ELSE 'FREE' END AS current_plan "
    sql += "FROM businesses b JOIN users u ON u.id=b.owner_user_id LEFT JOIN subscriptions s ON s.business_id=b.id WHERE 1=1"
    params: list[Any] = [iso_utc()]
    if search.strip():
        like = f"%{search.strip()[:150]}%"
        sql += " AND (lower(b.name) LIKE lower(?) OR lower(u.full_name) LIKE lower(?) OR lower(u.email) LIKE lower(?))"
        params.extend([like, like, like])
    if plan:
        sql += " AND (CASE WHEN s.status='ACTIVE' AND s.expiry_date>? THEN s.plan_id ELSE 'FREE' END)=?"
        params.extend([iso_utc(), plan.upper()])
    sql += " ORDER BY b.created_at DESC LIMIT 1000"
    return {"items": [dict(r) for r in db.execute(sql, tuple(params)).fetchall()]}


@router.get("/businesses/{business_id}")
def business_detail(business_id: str, admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    _admin_run_jobs(db)
    business = db.execute("SELECT b.*,u.full_name AS owner_name,u.email AS owner_email,u.phone AS owner_phone,u.account_status "
                          "FROM businesses b JOIN users u ON u.id=b.owner_user_id WHERE b.id=?", (business_id,)).fetchone()
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")
    subscription = db.execute("SELECT s.*,p.name AS plan_name,p.price AS plan_price FROM subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.business_id=?", (business_id,)).fetchone()
    members = db.execute("SELECT bm.role,u.full_name,u.email,u.phone,u.last_login,bm.joined_at FROM business_members bm JOIN users u ON u.id=bm.user_id WHERE bm.business_id=? ORDER BY bm.joined_at", (business_id,)).fetchall()
    counts = {}
    for table in ("products", "customers", "sales", "invoices", "expenses"):
        counts[table] = db.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE business_id=?", (business_id,)).fetchone()["n"]
    return {"business": dict(business), "subscription": dict(subscription) if subscription else None,
            "members": [dict(r) for r in members], "record_counts": counts}


@router.get("/payment-requests")
def list_payment_requests(
    status: str = "", plan_id: str = "", search: str = "", date_from: str = "", date_to: str = "",
    admin: dict = Depends(get_admin_user), db=Depends(get_db),
):
    sql = "SELECT pr.*,u.full_name,u.email,b.name AS business_name,p.name AS plan_name FROM payment_requests pr "
    sql += "JOIN users u ON u.id=pr.user_id JOIN businesses b ON b.id=pr.business_id JOIN plans p ON p.id=pr.plan_id WHERE 1=1"
    args: list[Any] = []
    if status:
        if status.upper() not in {"PENDING", "APPROVED", "REJECTED", "CANCELLED"}:
            raise HTTPException(status_code=422, detail="Choose a valid payment status.")
        sql += " AND pr.status=?"
        args.append(status.upper())
    if plan_id:
        sql += " AND pr.plan_id=?"
        args.append(plan_id.upper())
    if date_from:
        sql += " AND pr.submitted_at>=?"
        args.append(f"{date_from}T00:00:00Z")
    if date_to:
        sql += " AND pr.submitted_at<=?"
        args.append(f"{date_to}T23:59:59Z")
    if search.strip():
        like = f"%{search.strip()[:150]}%"
        sql += " AND (lower(u.email) LIKE lower(?) OR lower(b.name) LIKE lower(?) OR lower(pr.id) LIKE lower(?))"
        args.extend([like, like, like])
    sql += " ORDER BY CASE pr.status WHEN 'PENDING' THEN 0 ELSE 1 END,pr.submitted_at DESC LIMIT 1000"
    return {"items": [_admin_payment_row(r) for r in db.execute(sql, tuple(args)).fetchall()]}


@router.post("/payment-requests/{request_id}/approve")
def approve_payment_request(request_id: str, request: Request, payload: dict = Body(default={}),
                           admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    if payload.get("verified") is not True:
        raise HTTPException(status_code=422, detail="Confirm that you personally verified the bank transfer before approving.")
    now_dt = utc_now()
    now = iso_utc(now_dt)
    with transaction(db):
        lock_suffix = " FOR UPDATE" if getattr(db, "dialect", "sqlite") == "postgres" else ""
        payment = db.execute(f"SELECT * FROM payment_requests WHERE id=?{lock_suffix}", (request_id,)).fetchone()
        if not payment:
            raise HTTPException(status_code=404, detail="Payment request not found.")
        if payment["status"] == "APPROVED":
            raise HTTPException(status_code=409, detail="This payment request has already been approved.")
        if payment["status"] != "PENDING":
            raise HTTPException(status_code=409, detail=f"This payment request is {payment['status'].lower()} and cannot be approved.")
        plan = db.execute("SELECT * FROM plans WHERE id=? AND active=1", (payment["plan_id"],)).fetchone()
        if not plan or plan["id"] == "FREE":
            raise HTTPException(status_code=409, detail="The selected paid plan is no longer available.")
        if int(payment["amount"]) != int(plan["price"]):
            raise HTTPException(status_code=409, detail="Plan pricing changed after this request was submitted. Reject this request and ask the customer to submit a new one.")
        member = db.execute("SELECT role FROM business_members WHERE user_id=? AND business_id=?", (payment["user_id"], payment["business_id"])).fetchone()
        if not member or member["role"] != "OWNER":
            raise HTTPException(status_code=409, detail="The payment request is not associated with the business owner.")
        result = _activate_in_transaction(db, business_id=payment["business_id"], plan=plan, actor_id=admin["id"],
                                          duration_days=int(plan["duration_days"]), request_id=request_id,
                                          event_type="PAYMENT_APPROVED", now_dt=now_dt,
                                          details={"duration_days": int(plan["duration_days"]), "payment_method": "BANK_TRANSFER"})
        note = clean_text(payload.get("admin_note", ""), limit=1000, label="Admin note")
        db.execute("UPDATE payment_requests SET status='APPROVED',reviewed_at=?,reviewed_by=?,admin_note=? WHERE id=? AND status='PENDING'",
                   (now, admin["id"], note, request_id))
        db.execute("INSERT INTO payments(id,business_id,user_id,payment_request_id,amount,payment_method,status,paid_at,created_at) VALUES(?,?,?,?,?,'Bank Transfer','APPROVED',?,?)",
                   (make_id(), payment["business_id"], payment["user_id"], request_id, payment["amount"], now, now))
        add_notification(db, user_id=payment["user_id"], business_id=payment["business_id"], kind="SUBSCRIPTION_APPROVED",
                         message=f"Your {plan['name']} subscription is now active until {result['expiry_date']}.",
                         dedupe_key=f"payment-approved:{request_id}", now=now)
        add_audit(db, admin_id=admin["id"], target_user_id=payment["user_id"], target_business_id=payment["business_id"],
                  action="PAYMENT_APPROVED", description=f"Admin approved {plan['name']} bank-transfer payment request {request_id} for LKR {payment['amount']}; access expires {result['expiry_date']}.",
                  ip_address=_ip(request) if request else "", now=now)
    return {"ok": True, "status": "APPROVED", "plan": plan["id"], "start_date": result["start_date"], "expiry_date": result["expiry_date"],
            "message": f"{plan['name']} is active until {result['expiry_date']}."}


@router.post("/payment-requests/{request_id}/reject")
def reject_payment_request(request_id: str, request: Request, payload: dict = Body(...),
                           admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    try:
        note = clean_text(payload.get("admin_note"), limit=1000, required=True, label="Rejection reason")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    now = iso_utc()
    with transaction(db):
        lock_suffix = " FOR UPDATE" if getattr(db, "dialect", "sqlite") == "postgres" else ""
        payment = db.execute(f"SELECT * FROM payment_requests WHERE id=?{lock_suffix}", (request_id,)).fetchone()
        if not payment:
            raise HTTPException(status_code=404, detail="Payment request not found.")
        if payment["status"] == "APPROVED":
            raise HTTPException(status_code=409, detail="An approved payment request cannot be rejected.")
        if payment["status"] != "PENDING":
            raise HTTPException(status_code=409, detail=f"This payment request is already {payment['status'].lower()}.")
        db.execute("UPDATE payment_requests SET status='REJECTED',reviewed_at=?,reviewed_by=?,admin_note=? WHERE id=? AND status='PENDING'",
                   (now, admin["id"], note, request_id))
        add_notification(db, user_id=payment["user_id"], business_id=payment["business_id"], kind="PAYMENT_REJECTED",
                         message=f"Your payment request was rejected. Administrator note: {note}",
                         dedupe_key=f"payment-rejected:{request_id}", now=now)
        add_audit(db, admin_id=admin["id"], target_user_id=payment["user_id"], target_business_id=payment["business_id"],
                  action="PAYMENT_REJECTED", description=f"Admin rejected payment request {request_id}. Reason: {note}",
                  ip_address=_ip(request) if request else "", now=now)
    return {"ok": True, "status": "REJECTED", "message": "Payment request rejected."}


@router.post("/subscriptions/activate")
def manually_activate(request: Request, payload: dict = Body(...),
                      admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    business_id = clean_text(payload.get("business_id"), limit=80, required=True, label="Business")
    plan = _plan(db, clean_text(payload.get("plan_id"), limit=20, required=True, label="Plan"))
    if plan["id"] == "FREE":
        raise HTTPException(status_code=422, detail="Use Change plan to move a business to Free.")
    try:
        duration = int(payload.get("duration_days", plan["duration_days"] or 30))
        if duration < 1 or duration > 3650:
            raise ValueError("Duration must be between 1 and 3650 days.")
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Enter a valid duration between 1 and 3650 days.") from None
    reason = clean_text(payload.get("reason") or "Manual administrator activation", limit=500, label="Reason")
    custom_expiry = _timestamp(payload["custom_expiry"]) if payload.get("custom_expiry") else None
    now_dt, now = utc_now(), iso_utc()
    business = db.execute("SELECT id,owner_user_id FROM businesses WHERE id=?", (business_id,)).fetchone()
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")
    with transaction(db):
        result = _activate_in_transaction(db, business_id=business_id, plan=plan, actor_id=admin["id"], duration_days=duration,
                                          request_id=None, event_type="MANUAL_ACTIVATION", now_dt=now_dt,
                                          custom_expiry=custom_expiry, details={"duration_days": duration, "reason": reason})
        add_notification(db, user_id=business["owner_user_id"], business_id=business_id, kind="SUBSCRIPTION_ACTIVATED",
                         message=f"Your {plan['name']} subscription is active until {result['expiry_date']}.",
                         dedupe_key=f"manual-activation:{make_id()}", now=now)
        add_audit(db, admin_id=admin["id"], target_user_id=business["owner_user_id"], target_business_id=business_id,
                  action="MANUAL_ACTIVATION", description=f"Admin activated {plan['name']} for {duration} days. {reason}",
                  ip_address=_ip(request) if request else "", now=now)
    return {"ok": True, "start_date": result["start_date"], "expiry_date": result["expiry_date"]}


@router.post("/subscriptions/extend")
def extend_subscription(request: Request, payload: dict = Body(...),
                        admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    business_id = clean_text(payload.get("business_id"), limit=80, required=True, label="Business")
    try:
        days = int(payload.get("duration_days", 30))
        if days < 1 or days > 3650:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Enter a duration from 1 to 3650 days.") from None
    reason = clean_text(payload.get("reason") or f"Subscription extended by {days} days", limit=500, label="Reason")
    now_dt, now = utc_now(), iso_utc()
    business = db.execute("SELECT id,owner_user_id FROM businesses WHERE id=?", (business_id,)).fetchone()
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")
    with transaction(db):
        expire_one_in_transaction(db, business_id, now_dt=now_dt)
        sub = _ensure_subscription(db, business_id, now)
        plan_id = sub["plan_id"]
        if sub["status"] != "ACTIVE" or plan_id == "FREE":
            previous = db.execute("SELECT old_plan_id FROM subscription_events WHERE business_id=? AND event_type='EXPIRED' ORDER BY created_at DESC LIMIT 1", (business_id,)).fetchone()
            plan_id = previous["old_plan_id"] if previous and previous["old_plan_id"] not in (None, "FREE") else ""
        if not plan_id:
            raise HTTPException(status_code=409, detail="This business has no previous paid plan to extend. Use Activate Subscription and select a plan.")
        plan = _plan(db, plan_id)
        result = _activate_in_transaction(db, business_id=business_id, plan=plan, actor_id=admin["id"], duration_days=days,
                                          request_id=None, event_type="MANUAL_EXTEND", now_dt=now_dt,
                                          details={"duration_days": days, "reason": reason})
        add_notification(db, user_id=business["owner_user_id"], business_id=business_id, kind="SUBSCRIPTION_EXTENDED",
                         message=f"Your {plan['name']} subscription is active until {result['expiry_date']}.",
                         dedupe_key=f"manual-extend:{make_id()}", now=now)
        add_audit(db, admin_id=admin["id"], target_user_id=business["owner_user_id"], target_business_id=business_id,
                  action="SUBSCRIPTION_EXTENDED", description=f"Admin extended {plan['name']} by {days} days. {reason}",
                  ip_address=_ip(request) if request else "", now=now)
    return {"ok": True, "expiry_date": result["expiry_date"], "start_date": result["start_date"]}


@router.post("/subscriptions/change-plan")
def change_plan(request: Request, payload: dict = Body(...),
                admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    business_id = clean_text(payload.get("business_id"), limit=80, required=True, label="Business")
    plan_id = clean_text(payload.get("plan_id"), limit=20, required=True, label="Plan").upper()
    reason = clean_text(payload.get("reason"), limit=600, required=True, label="Reason")
    try:
        duration = int(payload.get("duration_days", 30))
        if duration < 1 or duration > 3650:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Duration must be between 1 and 3650 days.") from None
    plan = _plan(db, plan_id)
    business = db.execute("SELECT id,owner_user_id FROM businesses WHERE id=?", (business_id,)).fetchone()
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")
    now_dt, now = utc_now(), iso_utc()
    with transaction(db):
        expire_one_in_transaction(db, business_id, now_dt=now_dt)
        sub = _ensure_subscription(db, business_id, now)
        old = dict(sub)
        if plan_id == "FREE":
            db.execute("UPDATE subscriptions SET plan_id='FREE',status='FREE',start_date=NULL,expiry_date=NULL,updated_at=? WHERE id=?", (now, sub["id"]))
            new_start = new_expiry = None
            new_status = "FREE"
        elif sub["status"] == "ACTIVE" and parse_dt(sub["expiry_date"]) and parse_dt(sub["expiry_date"]) > now_dt:
            new_start, new_expiry, new_status = sub["start_date"], sub["expiry_date"], "ACTIVE"
            db.execute("UPDATE subscriptions SET plan_id=?,status='ACTIVE',updated_at=? WHERE id=?", (plan_id, now, sub["id"]))
        else:
            new_start, new_expiry, new_status = now, iso_utc(now_dt + timedelta(days=duration)), "ACTIVE"
            db.execute("UPDATE subscriptions SET plan_id=?,status='ACTIVE',start_date=?,expiry_date=?,updated_at=? WHERE id=?",
                       (plan_id, new_start, new_expiry, now, sub["id"]))
        db.execute("INSERT INTO subscription_events(id,idempotency_key,business_id,subscription_id,event_type,old_plan_id,new_plan_id,old_status,new_status,old_start_date,old_expiry_date,new_start_date,new_expiry_date,actor_user_id,details,created_at) "
                   "VALUES(?,?,?,?,'PLAN_CHANGED',?,?,?,?,?,?,?,?,?,?,?)",
                   (make_id(), f"change-plan:{make_id()}", business_id, sub["id"], old["plan_id"], plan_id, old["status"], new_status,
                    old["start_date"], old["expiry_date"], new_start, new_expiry, admin["id"], json.dumps({"reason": reason, "duration_days": duration}), now))
        add_notification(db, user_id=business["owner_user_id"], business_id=business_id, kind="PLAN_CHANGED",
                         message=f"Your plan was changed to {plan['name']} by the administrator.", dedupe_key=f"plan-change:{make_id()}", now=now)
        add_audit(db, admin_id=admin["id"], target_user_id=business["owner_user_id"], target_business_id=business_id,
                  action="PLAN_CHANGED", description=f"Admin changed plan from {old['plan_id']} to {plan_id}. Reason: {reason}",
                  ip_address=_ip(request) if request else "", now=now)
    return {"ok": True, "old_plan": old["plan_id"], "new_plan": plan_id, "status": new_status, "start_date": new_start, "expiry_date": new_expiry}


@router.post("/users/{user_id}/suspend")
def suspend_user(user_id: str, request: Request, payload: dict = Body(default={}),
                 admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    user = db.execute("SELECT * FROM users WHERE id=? AND role='CUSTOMER'", (user_id,)).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="Customer not found.")
    reason = clean_text(payload.get("reason") or "Suspended by administrator", limit=500, label="Reason")
    now = iso_utc()
    business = _user_business(db, user_id)
    with transaction(db):
        db.execute("UPDATE users SET account_status='SUSPENDED',updated_at=? WHERE id=?", (now, user_id))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        add_audit(db, admin_id=admin["id"], target_user_id=user_id, target_business_id=business["id"] if business else None,
                  action="ACCOUNT_SUSPENDED", description=f"Admin suspended account. {reason}", ip_address=_ip(request) if request else "", now=now)
    return {"ok": True, "account_status": "SUSPENDED"}


@router.post("/users/{user_id}/reactivate")
def reactivate_user(user_id: str, request: Request, payload: dict = Body(default={}),
                    admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    user = db.execute("SELECT * FROM users WHERE id=? AND role='CUSTOMER'", (user_id,)).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="Customer not found.")
    reason = clean_text(payload.get("reason") or "Reactivated by administrator", limit=500, label="Reason")
    now = iso_utc()
    business = _user_business(db, user_id)
    with transaction(db):
        db.execute("UPDATE users SET account_status='ACTIVE',updated_at=? WHERE id=?", (now, user_id))
        add_audit(db, admin_id=admin["id"], target_user_id=user_id, target_business_id=business["id"] if business else None,
                  action="ACCOUNT_REACTIVATED", description=f"Admin reactivated account. {reason}", ip_address=_ip(request) if request else "", now=now)
    return {"ok": True, "account_status": "ACTIVE"}


@router.get("/settings/payment")
def payment_settings(admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    row = db.execute("SELECT * FROM payment_settings WHERE id=1").fetchone()
    return dict(row) if row else {}


@router.put("/settings/payment")
def update_payment_settings(request: Request, payload: dict = Body(...),
                            admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    try:
        bank_name = clean_text(payload.get("bank_name", ""), limit=120, label="Bank name")
        account_name = clean_text(payload.get("account_name", ""), limit=160, label="Account name")
        account_number = clean_text(payload.get("account_number", ""), limit=80, label="Account number")
        branch = clean_text(payload.get("branch", ""), limit=120, label="Branch")
        instructions = clean_text(payload.get("instructions", ""), limit=1200, label="Payment instructions")
        currency = clean_text(payload.get("currency", "LKR"), limit=3, label="Currency").upper()
        whatsapp = clean_text(payload.get("whatsapp_number", ""), limit=40, label="WhatsApp number")
        if currency not in {"LKR", "USD", "EUR", "GBP", "INR"}:
            raise ValueError("Choose a supported bank-account currency.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    now = iso_utc()
    with transaction(db):
        db.execute("UPDATE payment_settings SET bank_name=?,account_name=?,account_number=?,branch=?,instructions=?,currency=?,whatsapp_number=?,updated_at=? WHERE id=1",
                   (bank_name, account_name, account_number, branch, instructions, currency, whatsapp, now))
        add_audit(db, admin_id=admin["id"], action="PAYMENT_SETTINGS_UPDATED", description="Admin updated bank-transfer payment instructions.",
                  ip_address=_ip(request) if request else "", now=now)
    return {"ok": True, "message": "Payment settings saved."}


@router.get("/settings/general")
def general_settings(admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    row = db.execute("SELECT value FROM app_settings WHERE key='general'").fetchone()
    return json.loads(row["value"]) if row else {}


@router.put("/settings/general")
def update_general_settings(request: Request, payload: dict = Body(...),
                            admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    try:
        support_phone = clean_text(payload.get("support_phone", ""), limit=40, label="Support phone")
        support_whatsapp = clean_text(payload.get("support_whatsapp", payload.get("whatsapp_number", "")), limit=40, label="WhatsApp number")
        value = {
            "website_name": clean_text(payload.get("website_name", "BizFlow LK"), limit=120, required=True, label="Website name"),
            "business_name": clean_text(payload.get("business_name", "BizFlow LK"), limit=160, required=True, label="Business name"),
            "support_email": clean_text(payload.get("support_email", ""), limit=254, label="Support email"),
            "support_phone": clean_phone(support_phone) if support_phone else "",
            "support_whatsapp": clean_phone(support_whatsapp) if support_whatsapp else "",
            "support_message": clean_text(payload.get("support_message", "If you need help with your account, contact the BizFlow LK administrator."), limit=500, label="Support message"),
            "whatsapp_number": clean_phone(support_whatsapp) if support_whatsapp else "",
        }
        if value["support_email"]:
            value["support_email"] = clean_email(value["support_email"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    now = iso_utc()
    with transaction(db):
        db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('general',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                   (json.dumps(value), now))
        add_audit(db, admin_id=admin["id"], action="GENERAL_SETTINGS_UPDATED", description="Admin updated platform contact details.",
                  ip_address=_ip(request) if request else "", now=now)
    return {"ok": True, "message": "Platform settings saved."}


@router.get("/audit-logs")
def audit_logs(search: str = "", action: str = "", admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    sql = "SELECT l.*,a.full_name AS admin_name,t.full_name AS target_name,b.name AS business_name FROM admin_audit_logs l "
    sql += "JOIN users a ON a.id=l.admin_user_id LEFT JOIN users t ON t.id=l.target_user_id LEFT JOIN businesses b ON b.id=l.target_business_id WHERE 1=1"
    args: list[Any] = []
    if action:
        sql += " AND l.action=?"
        args.append(action.upper())
    if search.strip():
        like = f"%{search.strip()[:150]}%"
        sql += " AND (lower(l.description) LIKE lower(?) OR lower(COALESCE(t.email,'')) LIKE lower(?) OR lower(COALESCE(b.name,'')) LIKE lower(?))"
        args.extend([like, like, like])
    sql += " ORDER BY l.created_at DESC LIMIT 1000"
    return {"items": [dict(r) for r in db.execute(sql, tuple(args)).fetchall()]}


@router.get("/plans")
def admin_plans(admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    rows = db.execute("SELECT * FROM plans ORDER BY sort_order").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["features"] = json.loads(item["features"] or "[]")
        result.append(item)
    return {"plans": result}


@router.put("/plans/{plan_id}")
def update_plan(plan_id: str, request: Request, payload: dict = Body(...),
                admin: dict = Depends(get_admin_user), db=Depends(get_db)):
    plan = db.execute("SELECT * FROM plans WHERE id=?", (plan_id.upper(),)).fetchone()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found.")
    try:
        price = clean_money(payload.get("price", plan["price"]), label="Plan price")
        active = bool(payload.get("active", bool(plan["active"])))
        duration = int(payload.get("duration_days", plan["duration_days"]))
        if duration < 1 or duration > 3650:
            raise ValueError("Duration must be between 1 and 3650 days.")
        if plan_id.upper() == "FREE" and price != 0:
            raise ValueError("The Free plan must remain LKR 0.")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    now = iso_utc()
    with transaction(db):
        db.execute("UPDATE plans SET price=?,active=?,duration_days=?,updated_at=? WHERE id=?", (price, int(active), duration, now, plan_id.upper()))
        add_audit(db, admin_id=admin["id"], action="PLAN_CONFIG_UPDATED", description=f"Admin updated {plan_id.upper()} pricing/duration to {price} and {duration} days.",
                  ip_address=_ip(request) if request else "", now=now)
    return {"ok": True}
