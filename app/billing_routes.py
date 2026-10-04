from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.config import settings
from app.db import get_db, transaction
from app.deps import get_business_ctx, get_admin_user
from app.rate_limits import enforce_rate_limit
from app.security import clean_text, iso_utc, make_id
from app.storage import verify_raster_image, write_private_file
from app.services import add_notification, require_permission

router = APIRouter(prefix="/api", tags=["billing"])
ALLOWED_FILES = {
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".pdf": ("application/pdf", b"%PDF-"),
}


def _require_billing_owner(ctx: dict[str, Any]) -> str:
    require_permission(ctx, "billing")
    if ctx.get("membership_role") != "OWNER":
        raise HTTPException(status_code=403, detail="Only the business owner can manage billing.")
    return ctx["business_id"]


def _public_plan(row: Any) -> dict[str, Any]:
    import json
    plan = dict(row)
    plan["features"] = json.loads(plan["features"] or "[]")
    return plan


@router.get("/billing/plans")
def billing_plans(db=Depends(get_db)):
    rows = db.execute("SELECT * FROM plans WHERE active=1 ORDER BY sort_order").fetchall()
    return {"plans": [_public_plan(r) for r in rows]}


@router.get("/billing")
def billing(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require_billing_owner(ctx)
    subscription = ctx["subscription"]
    payment_settings = db.execute("SELECT bank_name,account_name,account_number,branch,instructions,currency,whatsapp_number FROM payment_settings WHERE id=1").fetchone()
    last_payment = db.execute("SELECT p.amount,p.payment_method,p.paid_at,p.payment_request_id,pr.plan_id FROM payments p LEFT JOIN payment_requests pr ON pr.id=p.payment_request_id "
                              "WHERE p.business_id=? AND p.payment_request_id IS NOT NULL AND p.status='APPROVED' ORDER BY p.paid_at DESC LIMIT 1",
                              (business_id,)).fetchone()
    history = db.execute("SELECT pr.*,pl.name AS plan_name FROM payment_requests pr JOIN plans pl ON pl.id=pr.plan_id WHERE pr.business_id=? ORDER BY pr.submitted_at DESC LIMIT 50",
                         (business_id,)).fetchall()
    previous_plan = None
    if subscription["status"] == "EXPIRED":
        event = db.execute("SELECT old_plan_id FROM subscription_events WHERE business_id=? AND event_type='EXPIRED' ORDER BY created_at DESC LIMIT 1", (business_id,)).fetchone()
        previous_plan = event["old_plan_id"] if event else None
    return {
        "current_plan": ctx["effective_plan_id"],
        "subscription": subscription,
        "plan": ctx["plan"],
        "previous_plan": previous_plan,
        "days_remaining": _days_remaining(subscription.get("expiry_date")) if hasattr(subscription, "get") else _days_remaining(subscription["expiry_date"]),
        "last_payment": dict(last_payment) if last_payment else None,
        "bank": dict(payment_settings) if payment_settings else {},
        "payment_requests": [_history_row(r) for r in history],
    }


def _days_remaining(expiry: str | None) -> int | None:
    if not expiry:
        return None
    from app.security import parse_dt, utc_now
    parsed = parse_dt(expiry)
    if not parsed:
        return None
    return max(0, int((parsed - utc_now()).total_seconds() + 86399) // 86400)


def _history_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["receipt_url"] = f"/api/receipts/{result['id']}"
    result.pop("receipt_path", None)
    return result


@router.get("/billing/history")
def billing_history(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require_billing_owner(ctx)
    rows = db.execute(
        "SELECT pr.*,pl.name AS plan_name,se.new_start_date AS activated_start,se.new_expiry_date AS activated_expiry "
        "FROM payment_requests pr JOIN plans pl ON pl.id=pr.plan_id "
        "LEFT JOIN subscription_events se ON se.payment_request_id=pr.id AND se.event_type='PAYMENT_APPROVED' "
        "WHERE pr.business_id=? ORDER BY pr.submitted_at DESC LIMIT 250", (business_id,),
    ).fetchall()
    return {"items": [_history_row(r) for r in rows]}


@router.post("/billing/payment-request")
async def submit_payment_request(
    request: Request,
    plan_id: str = Form(...),
    receipt: UploadFile = File(...),
    ctx: dict = Depends(get_business_ctx),
    db=Depends(get_db),
):
    business_id = _require_billing_owner(ctx)
    enforce_rate_limit(request, scope="billing-payment-request", subject=business_id,
                       limit=3, window_seconds=3600)
    plan_id = clean_text(plan_id, limit=20, required=True, label="Plan").upper()
    if plan_id == "FREE":
        raise HTTPException(status_code=422, detail="The Free plan does not need a payment request.")
    plan = db.execute("SELECT * FROM plans WHERE id=? AND active=1", (plan_id,)).fetchone()
    if not plan:
        raise HTTPException(status_code=404, detail="This plan is not available.")
    pending = db.execute("SELECT id FROM payment_requests WHERE business_id=? AND status='PENDING' LIMIT 1", (business_id,)).fetchone()
    if pending:
        raise HTTPException(status_code=409, detail="This business already has a pending payment request. Wait for its review or cancel it before submitting another.")
    bank = db.execute("SELECT account_number FROM payment_settings WHERE id=1").fetchone()
    if not bank or not bank["account_number"]:
        raise HTTPException(status_code=409, detail="Bank transfer details are not configured yet. Please contact support.")
    filename = (receipt.filename or "").replace("\\", "/").split("/")[-1]
    ext = Path(filename).suffix.lower()
    allowed = ALLOWED_FILES.get(ext)
    if not allowed:
        raise HTTPException(status_code=415, detail="Upload a JPG, JPEG, PNG, or PDF receipt.")
    expected_mime, signature = allowed
    if receipt.content_type != expected_mime:
        raise HTTPException(status_code=415, detail="The receipt file type does not match its extension.")
    max_bytes = settings.max_receipt_mb * 1024 * 1024
    content = await receipt.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Receipt must be {settings.max_receipt_mb} MB or smaller.")
    if not content.startswith(signature):
        raise HTTPException(status_code=415, detail="The uploaded file does not appear to be a valid receipt.")
    if ext != ".pdf" and not verify_raster_image(content, ext):
        raise HTTPException(status_code=415, detail="The uploaded image could not be decoded as a valid JPG or PNG.")
    if ext == ".pdf" and b"%%EOF" not in content[-2048:]:
        raise HTTPException(status_code=415, detail="This PDF receipt appears to be incomplete.")
    receipt_id = make_id()
    private_dir = settings.storage_dir / "receipts"
    stored_path = private_dir / f"{receipt_id}{ext}"
    original_name = Path(filename).name[:120] or f"receipt{ext}"
    try:
        write_private_file(stored_path, content)
        now = iso_utc()
        with transaction(db):
            db.execute(
                "INSERT INTO payment_requests(id,user_id,business_id,plan_id,amount,currency,receipt_path,receipt_mime,receipt_name,status,submitted_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'PENDING',?)",
                (receipt_id, ctx["user"]["id"], business_id, plan_id, int(plan["price"]), "LKR", str(stored_path), expected_mime, original_name, now),
            )
            add_notification(db, user_id=ctx["user"]["id"], business_id=business_id, kind="PAYMENT_PENDING",
                             message="Your bank-transfer payment request has been submitted and is awaiting administrator verification.",
                             dedupe_key=f"payment-submitted:{receipt_id}", now=now)
    except Exception as exc:
        try:
            stored_path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, sqlite3.IntegrityError) or getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="This business already has a pending payment request. Wait for its review or cancel it before submitting another.") from None
        raise
    return {"ok": True, "id": receipt_id, "status": "PENDING", "message": "Your payment request has been submitted. Your paid plan will start only after an administrator verifies and approves the bank transfer."}


@router.post("/billing/payment-requests/{request_id}/cancel")
def cancel_payment_request(request_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require_billing_owner(ctx)
    now = iso_utc()
    with transaction(db):
        row = db.execute("SELECT * FROM payment_requests WHERE id=? AND business_id=? AND user_id=?", (request_id, business_id, ctx["user"]["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Payment request not found.")
        if row["status"] != "PENDING":
            raise HTTPException(status_code=409, detail=f"This payment request is already {row['status'].lower()}.")
        db.execute("UPDATE payment_requests SET status='CANCELLED',reviewed_at=?,admin_note='Cancelled by customer' WHERE id=? AND status='PENDING'", (now, request_id))
    return {"ok": True}


@router.get("/receipts/{request_id}")
def view_receipt(
    request_id: str,
    ctx: dict = Depends(get_business_ctx),
    db=Depends(get_db),
):
    # Unlike ordinary session lookup, the business-context dependency also blocks
    # suspended accounts and sessions that still require a temporary-password change.
    user = ctx["user"]
    row = db.execute("SELECT * FROM payment_requests WHERE id=?", (request_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    allowed = user.get("role") == "SUPER_ADMIN" or row["user_id"] == user["id"]
    if not allowed:
        raise HTTPException(status_code=403, detail="You are not allowed to view this receipt.")
    path = Path(row["receipt_path"])
    if not path.is_file() or settings.storage_dir.resolve() not in path.resolve().parents:
        raise HTTPException(status_code=404, detail="Receipt file is unavailable.")
    safe_name = quote(row["receipt_name"].replace("\r", "").replace("\n", ""))
    return FileResponse(
        path, media_type=row["receipt_mime"],
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{safe_name}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )
