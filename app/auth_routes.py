from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from app.account_security import encrypt_identity_number, generate_account_identifiers, identity_lookup_hash, normalize_identity_number
from app.db import get_db, transaction
from app.rate_limits import enforce_rate_limit
from app.deps import SESSION_COOKIE, clear_session_cookie, create_session, get_current_user, public_user
from app.security import (clean_email, clean_phone, clean_text, digest_secret, hash_password,
                          iso_utc, make_id, parse_dt, password_needs_rehash, utc_now, verify_password)
from app.services import add_notification, create_business_for_user, get_business_context

router = APIRouter(prefix="/api/auth", tags=["authentication"])


def _is_unique_violation(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.IntegrityError) or getattr(exc, "sqlstate", None) == "23505"


@router.post("/register")
def register(request: Request, response: Response, payload: dict = Body(...), db=Depends(get_db)):
    try:
        name = clean_text(payload.get("full_name"), limit=120, required=True, label="Full Name")
        identity_number = normalize_identity_number(payload.get("identity_number"))
        email = clean_email(payload.get("email"))
        phone = clean_phone(payload.get("phone"))
        password = payload.get("password", "")
        confirmation = payload.get("confirm_password")
        if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
            raise ValueError("Password must be between 10 and 128 characters.")
        if not isinstance(confirmation, str) or password != confirmation:
            raise ValueError("Passwords do not match.")
        business_name = clean_text(payload.get("business_name") or f"{name}'s Business", limit=160, required=True, label="Business name")
        business_type = clean_text(payload.get("business_type") or "Other", limit=80, label="Business type")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    identity_hash = identity_lookup_hash(identity_number)
    enforce_rate_limit(request, scope="auth-register", subject=digest_secret(f"{email}:{identity_hash}"),
                       limit=4, window_seconds=3600)
    if db.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
        raise HTTPException(status_code=409, detail="An account with this email already exists. Please sign in or contact the administrator if you need help.")
    if db.execute("SELECT id FROM users WHERE identity_number_lookup_hash=?", (identity_hash,)).fetchone():
        raise HTTPException(status_code=409, detail="This identity number is already associated with an account. Please contact the administrator if you believe this is an error.")

    password_hash = hash_password(password)
    now = iso_utc()
    user_id = make_id()
    try:
        with transaction(db):
            # Re-check inside the write transaction; unique database indexes are the final race-safe guard.
            if db.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
                raise HTTPException(status_code=409, detail="An account with this email already exists. Please sign in or contact the administrator if you need help.")
            if db.execute("SELECT id FROM users WHERE identity_number_lookup_hash=?", (identity_hash,)).fetchone():
                raise HTTPException(status_code=409, detail="This identity number is already associated with an account. Please contact the administrator if you believe this is an error.")
            username, public_account_id = generate_account_identifiers(db)
            db.execute(
                "INSERT INTO users(id,public_account_id,full_name,username,identity_number_encrypted,identity_number_lookup_hash,email,phone,password_hash,must_change_password,role,account_status,last_login,last_login_at,password_changed_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,0,'CUSTOMER','ACTIVE',?,?,?,?,?)",
                (user_id, public_account_id, name, username, encrypt_identity_number(identity_number), identity_hash,
                 email, phone, password_hash, now, now, now, now, now),
            )
            business_id = create_business_for_user(db, user_id, business_name, business_type, phone, email)
            add_notification(db, user_id=user_id, business_id=business_id, kind="WELCOME",
                             message="Welcome to BizFlow LK. Your Free plan is ready to use.",
                             dedupe_key=f"welcome:{user_id}", now=now)
            csrf = create_session(db, response, request, user_id)
    except Exception as exc:
        if _is_unique_violation(exc):
            if db.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
                raise HTTPException(status_code=409, detail="An account with this email already exists. Please sign in or contact the administrator if you need help.") from None
            if db.execute("SELECT id FROM users WHERE identity_number_lookup_hash=?", (identity_hash,)).fetchone():
                raise HTTPException(status_code=409, detail="This identity number is already associated with an account. Please contact the administrator if you believe this is an error.") from None
        raise
    return {
        "ok": True,
        "csrf_token": csrf,
        "next": "/dashboard",
        "username": username,
        "public_account_id": public_account_id,
        "email": email,
    }


@router.post("/login")
def login(request: Request, response: Response, payload: dict = Body(...), db=Depends(get_db)):
    try:
        identifier = clean_text(payload.get("identifier"), limit=254, required=True, label="Username or email")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    password = payload.get("password", "")
    if not isinstance(password, str):
        password = ""
    lookup = identifier.lower() if "@" in identifier else identifier.strip().upper()
    enforce_rate_limit(request, scope="auth-login", subject=lookup, limit=8, window_seconds=900)
    user = db.execute(
        "SELECT * FROM users WHERE lower(email)=lower(?) OR upper(COALESCE(username,''))=upper(?) LIMIT 1",
        (lookup, lookup),
    ).fetchone()
    if not user or not user["password_hash"] or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Username/email or password is incorrect.")

    now = iso_utc()
    upgraded_hash = hash_password(password) if password_needs_rehash(user["password_hash"]) else None
    with transaction(db):
        if upgraded_hash:
            db.execute("UPDATE users SET password_hash=?,last_login=?,last_login_at=?,updated_at=? WHERE id=?",
                       (upgraded_hash, now, now, now, user["id"]))
        else:
            db.execute("UPDATE users SET last_login=?,last_login_at=?,updated_at=? WHERE id=?",
                       (now, now, now, user["id"]))
        csrf = create_session(db, response, request, user["id"])
    return {
        "ok": True,
        "csrf_token": csrf,
        "next": "/change-password" if user["must_change_password"] else "/dashboard",
        "must_change_password": bool(user["must_change_password"]),
        "account_status": user["account_status"],
        "username": user["username"],
        "public_account_id": user["public_account_id"],
    }


@router.post("/logout")
def logout(request: Request, response: Response, user: dict = Depends(get_current_user), db=Depends(get_db)):
    raw = request.cookies.get(SESSION_COOKIE, "")
    with transaction(db):
        db.execute("DELETE FROM sessions WHERE token_hash=?", (digest_secret(raw),))
    clear_session_cookie(response, request)
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(get_current_user), db=Depends(get_db)):
    ctx = get_business_context(db, user, allow_suspended=True)
    if ctx.get("is_admin"):
        return {
            "user": public_user(user), "is_admin": True, "csrf_token": user["_csrf_token"],
            "account_status": user["account_status"], "must_change_password": bool(user["must_change_password"]),
        }
    previous_plan = None
    if ctx["subscription"]["status"] == "EXPIRED":
        event = db.execute(
            "SELECT old_plan_id FROM subscription_events WHERE business_id=? AND event_type='EXPIRED' ORDER BY created_at DESC LIMIT 1",
            (ctx["business_id"],),
        ).fetchone()
        previous_plan = event["old_plan_id"] if event else None
    return {
        "user": public_user(user),
        "business": ctx["business"],
        "membership_role": ctx["membership_role"],
        "is_admin": False,
        "account_status": user["account_status"],
        "must_change_password": bool(user["must_change_password"]),
        "subscription": ctx["subscription"],
        "current_plan": ctx["effective_plan_id"],
        "plan": ctx["plan"],
        "features": ctx["features"],
        "previous_plan": previous_plan,
        "csrf_token": user["_csrf_token"],
    }


@router.post("/change-password")
def change_password(
    request: Request,
    response: Response,
    payload: dict = Body(...),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    enforce_rate_limit(request, scope="auth-password-change", subject=user["id"], limit=6, window_seconds=3600)
    old_password = payload.get("current_password", "")
    new_password = payload.get("new_password", "")
    confirmation = payload.get("confirm_password")
    if not isinstance(confirmation, str) or confirmation != new_password:
        raise HTTPException(status_code=422, detail="Passwords do not match.")
    stored = db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not isinstance(old_password, str) or not verify_password(old_password, stored["password_hash"]):
        raise HTTPException(status_code=400, detail="Your current password is incorrect.")
    try:
        encoded = hash_password(new_password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    now = iso_utc()
    with transaction(db):
        db.execute("UPDATE users SET password_hash=?,password_changed_at=?,must_change_password=0,updated_at=? WHERE id=?",
                   (encoded, now, now, user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        csrf = create_session(db, response, request, user["id"])
    return {"ok": True, "message": "Your password has been updated.", "csrf_token": csrf}
