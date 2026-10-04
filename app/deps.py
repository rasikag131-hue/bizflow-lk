from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response

from app.config import settings
from app.db import get_db
from app.security import digest_secret, iso_utc, new_secret, parse_dt, utc_now
from app.services import get_business_context

SESSION_COOKIE = "bizflow_session"
ADMIN_PERMISSIONS = {"SUPER_ADMIN": frozenset({"VIEW_IDENTITY_NUMBER"})}


def csrf_for_session(raw_token: str) -> str:
    return hmac.new(settings.session_secret.encode(), ("csrf:" + raw_token).encode(), hashlib.sha256).hexdigest()


def _is_embedded_preview(request: Request) -> bool:
    if settings.is_deployment:
        return False
    candidates = [request.headers.get("x-forwarded-host", ""), request.headers.get("host", "")]
    for header in ("origin", "referer"):
        value = request.headers.get(header, "")
        if value:
            candidates.append(urlsplit(value).hostname or "")
    for value in candidates:
        host = value.split(",")[0].strip().lower().split(":", 1)[0]
        if host.endswith(".e2b.app"):
            return True
    return False


def cookie_samesite(request: Request) -> str:
    # Arena embeds preview apps cross-site. Browsers require SameSite=None; Secure
    # for auth cookies to work inside that iframe. Regular deployments remain Lax.
    return "none" if _is_embedded_preview(request) else "lax"


def secure_cookie(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return settings.is_deployment or request.url.scheme == "https" or forwarded == "https" or _is_embedded_preview(request)


def create_session(db: Any, response: Response, request: Request, user_id: str) -> str:
    raw_token = new_secret()
    csrf = csrf_for_session(raw_token)
    now = utc_now()
    expires = iso_utc(now + timedelta(days=30))
    db.execute(
        "INSERT INTO sessions(token_hash,csrf_hash,user_id,expires_at,created_at) VALUES(?,?,?,?,?)",
        (digest_secret(raw_token), digest_secret(csrf), user_id, expires, iso_utc(now)),
    )
    response.set_cookie(
        SESSION_COOKIE, raw_token, httponly=True, secure=secure_cookie(request),
        samesite=cookie_samesite(request), max_age=30 * 24 * 60 * 60, path="/", 
    )
    return csrf


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure_cookie(request), httponly=True, samesite=cookie_samesite(request))


def get_current_user(request: Request, db: Any = Depends(get_db)) -> dict[str, Any]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    session = db.execute(
        "SELECT s.csrf_hash,s.user_id,s.expires_at,u.id,u.public_account_id,u.full_name,u.username,u.email,u.phone,u.password_hash,u.must_change_password,u.avatar_url,u.role,u.account_status,COALESCE(u.last_login_at,u.last_login) AS last_login,u.created_at "
        "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
        (digest_secret(raw),),
    ).fetchone()
    if not session or not parse_dt(session["expires_at"]) or parse_dt(session["expires_at"]) <= utc_now():
        raise HTTPException(status_code=401, detail="Your session has expired. Please sign in again.")
    csrf = csrf_for_session(raw)
    if not hmac.compare_digest(session["csrf_hash"], digest_secret(csrf)):
        raise HTTPException(status_code=401, detail="Your session is no longer valid. Please sign in again.")
    user = dict(session)
    user.pop("csrf_hash", None)
    user.pop("expires_at", None)
    user.pop("password_hash", None)
    user["_csrf_token"] = csrf
    return user


def get_business_ctx(
    user: dict[str, Any] = Depends(get_current_user),
    db: Any = Depends(get_db),
) -> dict[str, Any]:
    if user.get("must_change_password"):
        raise HTTPException(status_code=403, detail="Please set a new password before continuing.", headers={"X-BizFlow-Error": "PASSWORD_CHANGE_REQUIRED"})
    return get_business_context(db, user)


def get_admin_user(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    if user.get("must_change_password"):
        raise HTTPException(status_code=403, detail="Please set a new password before continuing.", headers={"X-BizFlow-Error": "PASSWORD_CHANGE_REQUIRED"})
    return user


def require_admin_permission(user: dict[str, Any], permission: str) -> None:
    granted = ADMIN_PERMISSIONS.get(user.get("role"), frozenset())
    if permission not in granted:
        raise HTTPException(status_code=403, detail="You are not authorized to view this sensitive information.")


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    private_fields = {"password_hash", "identity_number", "identity_number_encrypted", "identity_number_lookup_hash"}
    return {k: v for k, v in user.items() if not k.startswith("_") and k not in private_fields}
