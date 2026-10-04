from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from fastapi import HTTPException

from app.db import transaction
from app.account_security import generate_account_identifiers
from app.security import clean_email, hash_password, iso_utc, make_id, parse_dt, utc_now

PLAN_SEEDS = [
    ("FREE", "Free", 0, 0, 10, 50, 1,
     ["dashboard", "products", "inventory", "sales", "customers", "basic_reports", "invoices"]),
    ("STARTER", "Starter", 1500, 30, 50, -1, 2,
     ["dashboard", "products", "inventory", "sales", "customers", "suppliers", "quotations", "invoices",
      "expense_tracking", "sales_reports", "pdf_invoices", "whatsapp_sharing"]),
    ("BUSINESS", "Business", 3500, 30, -1, -1, -1,
     ["dashboard", "products", "inventory", "sales", "customers", "suppliers", "quotations", "invoices",
      "expense_tracking", "sales_reports", "pdf_invoices", "whatsapp_sharing", "advanced_reports",
      "profit_overview", "low_stock_alerts", "sales_analytics", "expense_analytics", "customer_analytics",
      "multiple_locations", "data_export", "priority_support"]),
]


def seed_database(db: Any, settings: Any) -> None:
    now = iso_utc()
    with transaction(db):
        for sort_order, seed in enumerate(PLAN_SEEDS):
            plan_id, name, price, duration, product_limit, invoice_limit, staff_limit, features = seed
            db.execute(
                "INSERT INTO plans(id,name,price,duration_days,product_limit,invoice_limit,staff_limit,features,active,sort_order,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,product_limit=excluded.product_limit,invoice_limit=excluded.invoice_limit,staff_limit=excluded.staff_limit,features=excluded.features,sort_order=excluded.sort_order,updated_at=excluded.updated_at",
                (plan_id, name, price, duration, product_limit, invoice_limit, staff_limit,
                 json.dumps(features), sort_order, now),
            )
        db.execute(
            "INSERT INTO payment_settings(id,bank_name,account_name,account_number,branch,instructions,currency,whatsapp_number,updated_at) "
            "VALUES(1,'','','','','','LKR','',?) ON CONFLICT(id) DO NOTHING", (now,)
        )
        db.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
            ("general", json.dumps({
                "website_name": "BizFlow LK", "business_name": "BizFlow LK", "support_email": "",
                "support_phone": settings.default_support_phone, "support_whatsapp": settings.default_support_phone,
                "support_message": "If you need help with your account, contact the BizFlow LK administrator.",
                "whatsapp_number": settings.default_support_phone,
            }), now),
        )
        admin = db.execute("SELECT id FROM users WHERE role='SUPER_ADMIN' LIMIT 1").fetchone()
        if not admin:
            if not settings.admin_email or not settings.admin_password:
                raise RuntimeError("No Super Admin exists. Set ADMIN_EMAIL and ADMIN_PASSWORD for the one-time bootstrap, then run the migration command.")
            try:
                admin_email = clean_email(settings.admin_email)
            except ValueError as exc:
                raise RuntimeError("ADMIN_EMAIL must be a valid email address for initial administrator setup.") from exc
            if len(settings.admin_password) < 14 or len(settings.admin_password) > 128:
                raise RuntimeError("ADMIN_PASSWORD must be between 14 and 128 characters for initial administrator setup.")
            if db.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (admin_email,)).fetchone():
                raise RuntimeError("ADMIN_EMAIL is already assigned to a non-administrator account; choose a separate administrator email.")
            reserved_admin_id = db.execute("SELECT id FROM users WHERE lower(username)=lower('admin') OR upper(COALESCE(public_account_id,''))='USR-10000' LIMIT 1").fetchone()
            if reserved_admin_id:
                username, public_account_id = generate_account_identifiers(db)
            else:
                username, public_account_id = "admin", "USR-10000"
            db.execute(
                "INSERT INTO users(id,public_account_id,full_name,username,email,phone,password_hash,must_change_password,role,account_status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,0,'SUPER_ADMIN','ACTIVE',?,?)",
                (make_id(), public_account_id, "BizFlow Administrator", username, admin_email, "",
                 hash_password(settings.admin_password), now, now),
            )


def create_business_for_user(db: Any, user_id: str, name: str, business_type: str, phone: str = "", email: str = "") -> str:
    now = iso_utc()
    business_id = make_id()
    subscription_id = make_id()
    db.execute(
        "INSERT INTO businesses(id,name,business_type,owner_user_id,currency,phone,email,created_at,updated_at) "
        "VALUES(?,?,?,?, 'LKR',?,?,?,?)",
        (business_id, name, business_type or "Other", user_id, phone, email, now, now),
    )
    db.execute(
        "INSERT INTO business_members(id,business_id,user_id,role,joined_at) VALUES(?,?,?,'OWNER',?)",
        (make_id(), business_id, user_id, now),
    )
    db.execute(
        "INSERT INTO subscriptions(id,business_id,plan_id,status,start_date,expiry_date,created_at,updated_at) "
        "VALUES(?,?,'FREE','FREE',NULL,NULL,?,?)",
        (subscription_id, business_id, now, now),
    )
    return business_id


def add_notification(db: Any, *, user_id: str, business_id: str | None, kind: str,
                     message: str, dedupe_key: str | None = None, now: str | None = None) -> None:
    db.execute(
        "INSERT INTO notifications(id,user_id,business_id,notification_type,message,dedupe_key,created_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,dedupe_key) DO NOTHING",
        (make_id(), user_id, business_id, kind, message, dedupe_key or make_id(), now or iso_utc()),
    )


def add_audit(db: Any, *, admin_id: str, action: str, description: str,
              target_user_id: str | None = None, target_business_id: str | None = None,
              ip_address: str = "", now: str | None = None) -> None:
    db.execute(
        "INSERT INTO admin_audit_logs(id,admin_user_id,target_user_id,target_business_id,action,description,ip_address,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (make_id(), admin_id, target_user_id, target_business_id, action, description, ip_address[:100], now or iso_utc()),
    )


def expire_one_in_transaction(db: Any, business_id: str, *, now_dt=None) -> bool:
    current = now_dt or utc_now()
    now = iso_utc(current)
    lock_suffix = " FOR UPDATE" if getattr(db, "dialect", "sqlite") == "postgres" else ""
    sub = db.execute(f"SELECT * FROM subscriptions WHERE business_id=?{lock_suffix}", (business_id,)).fetchone()
    if not sub or sub["status"] != "ACTIVE":
        return False
    expiry = parse_dt(sub["expiry_date"])
    if not expiry or current < expiry:
        return False
    owner = db.execute("SELECT owner_user_id FROM businesses WHERE id=?", (business_id,)).fetchone()
    db.execute(
        "UPDATE subscriptions SET plan_id='FREE',status='EXPIRED',updated_at=? WHERE id=? AND status='ACTIVE'",
        (now, sub["id"]),
    )
    old_plan = sub["plan_id"]
    key = f"expire:{sub['id']}:{sub['expiry_date']}"
    db.execute(
        "INSERT INTO subscription_events(id,idempotency_key,business_id,subscription_id,event_type,old_plan_id,new_plan_id,old_status,new_status,old_start_date,old_expiry_date,new_start_date,new_expiry_date,actor_user_id,details,created_at) "
        "VALUES(?,?,?,?,'EXPIRED',?,'FREE','ACTIVE','EXPIRED',?,?,?,?,NULL,?,?) ON CONFLICT(idempotency_key) DO NOTHING",
        (make_id(), key, business_id, sub["id"], old_plan, sub["start_date"], sub["expiry_date"], sub["start_date"], sub["expiry_date"],
         json.dumps({"reason": "subscription_term_elapsed"}), now),
    )
    if owner:
        add_notification(
            db, user_id=owner["owner_user_id"], business_id=business_id, kind="SUBSCRIPTION_EXPIRED",
            message="Your subscription has expired and your account has moved to the Free plan.",
            dedupe_key=f"subexpired:{sub['id']}:{sub['expiry_date']}", now=now,
        )
    return True


def expire_business(db: Any, business_id: str) -> bool:
    with transaction(db):
        return expire_one_in_transaction(db, business_id)


def run_billing_jobs(db: Any) -> dict[str, int]:
    now_dt = utc_now()
    now = iso_utc(now_dt)
    expired_count = 0
    warning_count = 0
    with transaction(db):
        due = db.execute(
            "SELECT business_id FROM subscriptions WHERE status='ACTIVE' AND expiry_date IS NOT NULL AND expiry_date<=?",
            (now,),
        ).fetchall()
        for row in due:
            expired_count += int(expire_one_in_transaction(db, row["business_id"], now_dt=now_dt))

        active = db.execute(
            "SELECT s.id,s.business_id,s.expiry_date,b.owner_user_id,p.name AS plan_name "
            "FROM subscriptions s JOIN businesses b ON b.id=s.business_id JOIN plans p ON p.id=s.plan_id "
            "WHERE s.status='ACTIVE' AND s.expiry_date IS NOT NULL AND s.expiry_date>?",
            (now,),
        ).fetchall()
        for sub in active:
            expiry = parse_dt(sub["expiry_date"])
            if not expiry:
                continue
            days_exact = (expiry - now_dt).total_seconds() / 86400
            days_ceil = int(-(-days_exact // 1))
            if days_ceil not in (7, 3, 1):
                continue
            label = "tomorrow" if days_ceil == 1 else f"in {days_ceil} days"
            add_notification(
                db,
                user_id=sub["owner_user_id"],
                business_id=sub["business_id"],
                kind="SUBSCRIPTION_EXPIRING",
                message=f"Your {sub['plan_name']} subscription expires {label}.",
                dedupe_key=f"subwarn:{sub['id']}:{sub['expiry_date']}:{days_ceil}",
                now=now,
            )
            warning_count += 1
    return {"expired": expired_count, "warnings": warning_count}


def get_business_context(db: Any, user: dict[str, Any], *, allow_suspended: bool = False) -> dict[str, Any]:
    if user["role"] == "SUPER_ADMIN":
        return {"user": user, "is_admin": True, "business_id": None, "membership_role": None}
    if user["account_status"] != "ACTIVE" and not allow_suspended:
        raise HTTPException(status_code=403, detail="Your account has been suspended. Please contact support.")
    membership = db.execute(
        "SELECT bm.business_id,bm.role,b.name AS business_name,b.currency,b.owner_user_id,b.business_type,b.phone AS business_phone,b.email AS business_email,b.address,b.tax_details,b.invoice_prefix,b.invoice_notes "
        "FROM business_members bm JOIN businesses b ON b.id=bm.business_id WHERE bm.user_id=? ORDER BY bm.joined_at LIMIT 1",
        (user["id"],),
    ).fetchone()
    if not membership:
        raise HTTPException(status_code=403, detail="This account is not linked to a business.")
    expire_business(db, membership["business_id"])
    sub = db.execute("SELECT * FROM subscriptions WHERE business_id=?", (membership["business_id"],)).fetchone()
    if not sub:
        raise HTTPException(status_code=403, detail="Subscription information is unavailable.")
    expiry = parse_dt(sub["expiry_date"])
    effective_plan_id = sub["plan_id"] if sub["status"] == "ACTIVE" and expiry and expiry > utc_now() else "FREE"
    plan = db.execute("SELECT * FROM plans WHERE id=?", (effective_plan_id,)).fetchone()
    if not plan:
        raise HTTPException(status_code=403, detail="Plan configuration is unavailable.")
    return {
        "user": user,
        "is_admin": False,
        "business_id": membership["business_id"],
        "business": dict(membership),
        "membership_role": membership["role"],
        "subscription": dict(sub),
        "plan": dict(plan),
        "features": json.loads(plan["features"] or "[]"),
        "effective_plan_id": effective_plan_id,
    }


ROLE_PERMISSIONS = {
    "OWNER": {"*"},
    "MANAGER": {"dashboard", "products_read", "products_write", "inventory", "sales", "customers", "suppliers", "invoices", "quotations", "reports"},
    "STAFF": {"dashboard", "products_read", "sales", "customers"},
}


def require_permission(ctx: dict[str, Any], permission: str) -> None:
    if ctx.get("is_admin"):
        return
    role = ctx.get("membership_role")
    if permission not in ROLE_PERMISSIONS.get(role, set()) and "*" not in ROLE_PERMISSIONS.get(role, set()):
        raise HTTPException(status_code=403, detail="Your team role does not have permission to do this.")


def require_feature(ctx: dict[str, Any], feature: str, message: str | None = None) -> None:
    if feature not in ctx.get("features", []):
        raise HTTPException(status_code=402, detail=message or f"This feature is available on a paid BizFlow plan. Upgrade to continue.", headers={"X-BizFlow-Error": "PLAN_REQUIRED"})
