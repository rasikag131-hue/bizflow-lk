from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote
from typing import Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.config import settings
from app.account_security import (decrypt_identity_number, encrypt_identity_number, generate_account_identifiers,
                                  identity_lookup_hash, mask_identity_number, normalize_identity_number)
from app.db import get_db, transaction
from app.deps import get_business_ctx, get_current_user, public_user
from app.security import (clean_email, clean_money, clean_phone, clean_quantity, clean_text, hash_password,
                          iso_utc, make_id, new_secret, utc_now, verify_password)
from app.storage import verify_raster_image, write_private_file
from app.services import (add_notification, get_business_context, require_feature, require_permission)
from app.timeutils import date_range, local_today, utc_bounds

router = APIRouter(prefix="/api", tags=["business"])


def _bid(ctx: dict[str, Any]) -> str:
    if not ctx.get("business_id"):
        raise HTTPException(status_code=403, detail="A business account is required.")
    return ctx["business_id"]


def _require(ctx: dict[str, Any], permission: str) -> str:
    require_permission(ctx, permission)
    return _bid(ctx)


def _same_business(db: Any, table: str, row_id: str, business_id: str) -> Any:
    # The table name is selected only from internal route constants.
    return db.execute(f"SELECT * FROM {table} WHERE id=? AND business_id=?", (row_id, business_id)).fetchone()


def _customer_id(db: Any, business_id: str, value: Any) -> str | None:
    if value in (None, ""):
        return None
    customer = _same_business(db, "customers", str(value), business_id)
    if not customer or customer["archived"]:
        raise HTTPException(status_code=422, detail="Select a customer from this business.")
    return customer["id"]


def _supplier_id(db: Any, business_id: str, value: Any) -> str | None:
    if value in (None, ""):
        return None
    supplier = _same_business(db, "suppliers", str(value), business_id)
    if not supplier or supplier["archived"]:
        raise HTTPException(status_code=422, detail="Select a supplier from this business.")
    return supplier["id"]


@router.get("/dashboard")
def dashboard(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "dashboard")
    now = utc_now()
    today = local_today()
    today_start, tomorrow_start = utc_bounds(today, today + timedelta(days=1))
    month_start, month_end = utc_bounds(today.replace(day=1), today + timedelta(days=1))
    today_sales = db.execute(
        "SELECT COALESCE(SUM(total),0) AS total,COUNT(*) AS count FROM sales WHERE business_id=? AND created_at>=? AND created_at<?",
        (business_id, today_start, tomorrow_start),
    ).fetchone()
    month_sales = db.execute(
        "SELECT COALESCE(SUM(total),0) AS total,COALESCE(SUM(paid_amount),0) AS paid,COUNT(*) AS count FROM sales WHERE business_id=? AND created_at>=? AND created_at<?",
        (business_id, month_start, month_end),
    ).fetchone()
    if "expense_tracking" in ctx["features"]:
        month_expenses = db.execute(
            "SELECT COALESCE(SUM(amount),0) AS total,COUNT(*) AS count FROM expenses WHERE business_id=? AND expense_date>=? AND expense_date<?",
            (business_id, today.replace(day=1).isoformat(), (today + timedelta(days=1)).isoformat()),
        ).fetchone()
    else:
        month_expenses = {"total": 0, "count": 0}
    estimated_profit = None
    if "profit_overview" in ctx["features"]:
        cost = db.execute(
            "SELECT COALESCE(SUM(si.unit_cost*si.quantity),0) AS total FROM sale_items si JOIN sales s ON s.id=si.sale_id "
            "WHERE si.business_id=? AND s.created_at>=? AND s.created_at<?", (business_id, month_start, month_end),
        ).fetchone()["total"]
        estimated_profit = int(month_sales["total"]) - int(round(float(cost))) - int(month_expenses["total"])
    outstanding = db.execute("SELECT COALESCE(SUM(balance),0) AS total FROM invoices WHERE business_id=?", (business_id,)).fetchone()
    stock = db.execute(
        "SELECT COUNT(*) AS products,COALESCE(SUM(CASE WHEN quantity>0 AND quantity<=minimum_stock THEN 1 ELSE 0 END),0) AS low_stock,"
        "COALESCE(SUM(CASE WHEN quantity<=0 THEN 1 ELSE 0 END),0) AS out_stock,COALESCE(SUM(quantity*purchase_price),0) AS inventory_value "
        "FROM products WHERE business_id=? AND archived=0", (business_id,),
    ).fetchone()
    recent_sales = db.execute(
        "SELECT s.id,s.sale_number,s.total,s.status,s.created_at,COALESCE(c.name,'Walk-in customer') AS customer_name "
        "FROM sales s LEFT JOIN customers c ON c.id=s.customer_id WHERE s.business_id=? ORDER BY s.created_at DESC LIMIT 6",
        (business_id,),
    ).fetchall()
    top_products = db.execute(
        "SELECT si.product_name AS name,SUM(si.quantity) AS units,SUM(si.line_total) AS revenue "
        "FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE si.business_id=? AND s.created_at>=? AND s.created_at<? "
        "GROUP BY si.product_name ORDER BY revenue DESC LIMIT 5", (business_id, month_start, month_end),
    ).fetchall()
    daily = []
    for back in range(6, -1, -1):
        day = today - timedelta(days=back)
        start, end = utc_bounds(day, day + timedelta(days=1))
        amount = db.execute("SELECT COALESCE(SUM(total),0) AS total FROM sales WHERE business_id=? AND created_at>=? AND created_at<?",
                            (business_id, start, end)).fetchone()["total"]
        daily.append({"date": day.isoformat(), "amount": amount})
    expiring = None
    if ctx["subscription"]["status"] == "ACTIVE" and ctx["subscription"]["expiry_date"]:
        from app.security import parse_dt
        expiry = parse_dt(ctx["subscription"]["expiry_date"])
        expiring = max(0, int((expiry - now).total_seconds() // 86400)) if expiry else None
    return {
        "business": ctx["business"], "current_plan": ctx["effective_plan_id"], "subscription": ctx["subscription"],
        "today_sales": dict(today_sales), "month_sales": dict(month_sales), "month_expenses": dict(month_expenses),
        "estimated_profit": estimated_profit, "can_view_expenses": "expense_tracking" in ctx["features"],
        "outstanding_balance": outstanding["total"], "stock": dict(stock), "recent_sales": [dict(x) for x in recent_sales],
        "top_products": [dict(x) for x in top_products], "sales_last_7_days": daily,
        "expiry_days_remaining": expiring,
    }


@router.get("/features/{feature_name}")
def feature_access(feature_name: str, ctx: dict = Depends(get_business_ctx)):
    _bid(ctx)
    require_feature(ctx, feature_name, f"{feature_name.replace('_', ' ').title()} is not available on your current plan.")
    return {"allowed": True, "plan": ctx["effective_plan_id"], "feature": feature_name}


@router.get("/products")
def list_products(search: str = "", include_archived: bool = False, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "products_read")
    query = "SELECT p.*,s.name AS supplier_name FROM products p LEFT JOIN suppliers s ON s.id=p.supplier_id WHERE p.business_id=?"
    args: list[Any] = [business_id]
    if not include_archived:
        query += " AND p.archived=0"
    term = search.strip()[:100]
    if term:
        query += " AND (lower(p.name) LIKE lower(?) OR lower(p.sku) LIKE lower(?) OR lower(p.barcode) LIKE lower(?) OR lower(p.category) LIKE lower(?))"
        like = f"%{term}%"
        args.extend([like, like, like, like])
    query += " ORDER BY p.name LIMIT 1000"
    rows = db.execute(query, tuple(args)).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        has_image = bool(item.pop("image_path", ""))
        item["image_url"] = f"/api/products/{item['id']}/image" if has_image else ""
        qty = float(item["quantity"] or 0)
        minimum = float(item["minimum_stock"] or 0)
        item["stock_status"] = "OUT OF STOCK" if qty <= 0 else ("LOW STOCK" if qty <= minimum else "IN STOCK")
        output.append(item)
    return {"items": output, "count": len(output), "plan": ctx["effective_plan_id"]}


@router.post("/products")
def create_product(payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "products_write")
    try:
        name = clean_text(payload.get("name"), limit=160, required=True, label="Product name")
        sku = clean_text(payload.get("sku"), limit=64, label="SKU") or f"SKU-{make_id()[:8].upper()}"
        barcode = clean_text(payload.get("barcode"), limit=80, label="Barcode")
        category = clean_text(payload.get("category"), limit=100, label="Category")
        purchase = clean_money(payload.get("purchase_price", 0), label="Purchase price")
        selling = clean_money(payload.get("selling_price", 0), label="Selling price")
        quantity = clean_quantity(payload.get("quantity", 0), label="Quantity")
        minimum = clean_quantity(payload.get("minimum_stock", 0), label="Minimum stock")
        supplier_id = _supplier_id(db, business_id, payload.get("supplier_id"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    limit = int(ctx["plan"]["product_limit"])
    count = db.execute("SELECT COUNT(*) AS n FROM products WHERE business_id=? AND archived=0", (business_id,)).fetchone()["n"]
    if limit >= 0 and count >= limit:
        raise HTTPException(status_code=402, detail=f"Your Free plan allows up to {limit} products. Upgrade your plan to add more.", headers={"X-BizFlow-Error": "PLAN_LIMIT"})
    if db.execute("SELECT id FROM products WHERE business_id=? AND sku=? AND archived=0", (business_id, sku)).fetchone():
        raise HTTPException(status_code=409, detail="That SKU is already in use. Choose a different SKU.")
    now, product_id = iso_utc(), make_id()
    with transaction(db):
        db.execute(
            "INSERT INTO products(id,business_id,name,sku,barcode,category,purchase_price,selling_price,quantity,minimum_stock,supplier_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (product_id, business_id, name, sku, barcode, category, purchase, selling, quantity, minimum, supplier_id, now, now),
        )
        if quantity:
            db.execute(
                "INSERT INTO inventory_movements(id,business_id,product_id,movement_type,quantity_delta,previous_quantity,new_quantity,reason,user_id,created_at) "
                "VALUES(?,?,?,'ADJUSTMENT',?,0,?,'Opening stock',?,?)",
                (make_id(), business_id, product_id, quantity, quantity, ctx["user"]["id"], now),
            )
    return {"ok": True, "id": product_id, "message": "Product added."}


@router.put("/products/{product_id}")
def update_product(product_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "products_write")
    existing = _same_business(db, "products", product_id, business_id)
    if not existing or existing["archived"]:
        raise HTTPException(status_code=404, detail="Product not found.")
    try:
        name = clean_text(payload.get("name", existing["name"]), limit=160, required=True, label="Product name")
        sku = clean_text(payload.get("sku", existing["sku"]), limit=64, label="SKU") or existing["sku"]
        barcode = clean_text(payload.get("barcode", existing["barcode"]), limit=80, label="Barcode")
        category = clean_text(payload.get("category", existing["category"]), limit=100, label="Category")
        purchase = clean_money(payload.get("purchase_price", existing["purchase_price"]), label="Purchase price")
        selling = clean_money(payload.get("selling_price", existing["selling_price"]), label="Selling price")
        quantity = clean_quantity(payload.get("quantity", existing["quantity"]), label="Quantity")
        minimum = clean_quantity(payload.get("minimum_stock", existing["minimum_stock"]), label="Minimum stock")
        supplier_id = _supplier_id(db, business_id, payload.get("supplier_id", existing["supplier_id"]))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    duplicate = db.execute("SELECT id FROM products WHERE business_id=? AND sku=? AND id<>? AND archived=0", (business_id, sku, product_id)).fetchone()
    if duplicate:
        raise HTTPException(status_code=409, detail="That SKU is already in use.")
    now = iso_utc()
    with transaction(db):
        if float(quantity) != float(existing["quantity"]):
            delta = float(quantity) - float(existing["quantity"])
            db.execute(
                "INSERT INTO inventory_movements(id,business_id,product_id,movement_type,quantity_delta,previous_quantity,new_quantity,reason,user_id,created_at) "
                "VALUES(?,?,?,'ADJUSTMENT',?,?,?,?,?,?)",
                (make_id(), business_id, product_id, delta, existing["quantity"], quantity,
                 clean_text(payload.get("stock_reason") or "Product edit adjustment", limit=240), ctx["user"]["id"], now),
            )
        db.execute(
            "UPDATE products SET name=?,sku=?,barcode=?,category=?,purchase_price=?,selling_price=?,quantity=?,minimum_stock=?,supplier_id=?,updated_at=? WHERE id=? AND business_id=?",
            (name, sku, barcode, category, purchase, selling, quantity, minimum, supplier_id, now, product_id, business_id),
        )
    return {"ok": True, "message": "Product updated."}


@router.post("/products/{product_id}/image")
async def upload_product_image(product_id: str, image: UploadFile = File(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "products_write")
    product = _same_business(db, "products", product_id, business_id)
    if not product or product["archived"]:
        raise HTTPException(status_code=404, detail="Product not found.")
    filename = (image.filename or "").replace("\\", "/").split("/")[-1]
    extension = Path(filename).suffix.lower()
    accepted = {".jpg": ("image/jpeg", b"\xff\xd8\xff"), ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
                ".png": ("image/png", b"\x89PNG\r\n\x1a\n")}
    if extension not in accepted or image.content_type != accepted[extension][0]:
        raise HTTPException(status_code=415, detail="Upload a valid JPG or PNG product image.")
    content = await image.read(3 * 1024 * 1024 + 1)
    if len(content) > 3 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Product images must be 3 MB or smaller.")
    if not content.startswith(accepted[extension][1]) or not verify_raster_image(content, extension):
        raise HTTPException(status_code=415, detail="The uploaded image file could not be decoded as a valid JPG or PNG.")
    directory = settings.storage_dir / "products"
    path = directory / f"{business_id}-{product_id}-{make_id()}{extension}"
    write_private_file(path, content)
    now = iso_utc()
    old_path = product["image_path"]
    try:
        with transaction(db):
            db.execute("UPDATE products SET image_path=?,updated_at=? WHERE id=? AND business_id=?", (str(path), now, product_id, business_id))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if old_path:
        try:
            old = Path(old_path).resolve()
            if settings.storage_dir.resolve() in old.parents:
                old.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "image_url": f"/api/products/{product_id}/image"}


@router.get("/products/{product_id}/image")
def view_product_image(product_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "products_read")
    product = _same_business(db, "products", product_id, business_id)
    if not product or not product["image_path"]:
        raise HTTPException(status_code=404, detail="Product image not found.")
    path = Path(product["image_path"])
    if not path.is_file() or settings.storage_dir.resolve() not in path.resolve().parents:
        raise HTTPException(status_code=404, detail="Product image is unavailable.")
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    return FileResponse(path, media_type=mime, headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name)}",
                        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox",
                        "Cache-Control": "private, no-store"})


@router.delete("/products/{product_id}")
def archive_product(product_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "products_write")
    item = _same_business(db, "products", product_id, business_id)
    if not item or item["archived"]:
        raise HTTPException(status_code=404, detail="Product not found.")
    with transaction(db):
        db.execute("UPDATE products SET archived=1,updated_at=? WHERE id=? AND business_id=?", (iso_utc(), product_id, business_id))
    return {"ok": True, "message": "Product archived. Historical sales and invoices are unchanged."}


@router.get("/inventory")
def inventory(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "inventory")
    products = db.execute(
        "SELECT id,name,sku,category,quantity,minimum_stock,purchase_price,selling_price FROM products WHERE business_id=? AND archived=0 ORDER BY name",
        (business_id,),
    ).fetchall()
    movements = db.execute(
        "SELECT m.*,p.name AS product_name,u.full_name AS user_name FROM inventory_movements m JOIN products p ON p.id=m.product_id LEFT JOIN users u ON u.id=m.user_id "
        "WHERE m.business_id=? ORDER BY m.created_at DESC LIMIT 15", (business_id,),
    ).fetchall()
    rows = [dict(row) for row in products]
    return {
        "items": rows,
        "movements": [dict(row) for row in movements],
        "summary": {
            "products": len(rows),
            "low_stock": sum(1 for p in rows if 0 < float(p["quantity"]) <= float(p["minimum_stock"])),
            "out_of_stock": sum(1 for p in rows if float(p["quantity"]) <= 0),
            "inventory_value": sum(float(p["quantity"]) * int(p["purchase_price"]) for p in rows),
        },
    }


@router.post("/inventory/{product_id}/adjust")
def adjust_inventory(product_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "inventory")
    try:
        new_quantity = clean_quantity(payload.get("quantity"), label="New quantity")
        reason = clean_text(payload.get("reason"), limit=240, required=True, label="Reason")
        movement_type = clean_text(payload.get("movement_type", "ADJUSTMENT"), limit=20).upper()
        if movement_type not in {"PURCHASE", "RETURN", "ADJUSTMENT"}:
            raise ValueError("Choose Purchase, Return, or Adjustment.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    now = iso_utc()
    with transaction(db):
        lock_suffix = " FOR UPDATE" if getattr(db, "dialect", "sqlite") == "postgres" else ""
        product = db.execute(f"SELECT * FROM products WHERE id=? AND business_id=? AND archived=0{lock_suffix}", (product_id, business_id)).fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="Product not found.")
        previous = float(product["quantity"])
        delta = new_quantity - previous
        if movement_type in {"PURCHASE", "RETURN"} and delta <= 0:
            raise HTTPException(status_code=422, detail=f"A {movement_type.lower()} movement must increase the stock quantity.")
        db.execute("UPDATE products SET quantity=?,updated_at=? WHERE id=? AND business_id=?", (new_quantity, now, product_id, business_id))
        db.execute(
            "INSERT INTO inventory_movements(id,business_id,product_id,movement_type,quantity_delta,previous_quantity,new_quantity,reason,user_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (make_id(), business_id, product_id, movement_type, delta, previous, new_quantity, reason, ctx["user"]["id"], now),
        )
    return {"ok": True, "quantity": new_quantity}


@router.get("/customers")
def list_customers(search: str = "", ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "customers")
    sql = "SELECT * FROM customers WHERE business_id=? AND archived=0"
    args: list[Any] = [business_id]
    if search.strip():
        like = f"%{search.strip()[:100]}%"
        sql += " AND (lower(name) LIKE lower(?) OR lower(phone) LIKE lower(?) OR lower(email) LIKE lower(?))"
        args.extend([like, like, like])
    sql += " ORDER BY name LIMIT 1000"
    return {"items": [dict(r) for r in db.execute(sql, tuple(args)).fetchall()]}


@router.post("/customers")
def create_customer(payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "customers")
    try:
        name = clean_text(payload.get("name"), limit=140, required=True, label="Customer name")
        phone = clean_text(payload.get("phone"), limit=40, label="Phone")
        email = clean_text(payload.get("email"), limit=254, label="Email")
        address = clean_text(payload.get("address"), limit=500, label="Address")
        notes = clean_text(payload.get("notes"), limit=1000, label="Notes")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    customer_id, now = make_id(), iso_utc()
    with transaction(db):
        db.execute("INSERT INTO customers(id,business_id,name,phone,email,address,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (customer_id, business_id, name, phone, email, address, notes, now, now))
    return {"ok": True, "id": customer_id, "message": "Customer added."}


@router.put("/customers/{customer_id}")
def update_customer(customer_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "customers")
    customer = _same_business(db, "customers", customer_id, business_id)
    if not customer or customer["archived"]:
        raise HTTPException(status_code=404, detail="Customer not found.")
    try:
        values = [clean_text(payload.get(key, customer[old]), limit=limit, required=required, label=label) for key, old, limit, required, label in [
            ("name", "name", 140, True, "Customer name"), ("phone", "phone", 40, False, "Phone"),
            ("email", "email", 254, False, "Email"), ("address", "address", 500, False, "Address"), ("notes", "notes", 1000, False, "Notes")]]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    with transaction(db):
        db.execute("UPDATE customers SET name=?,phone=?,email=?,address=?,notes=?,updated_at=? WHERE id=? AND business_id=?",
                   (*values, iso_utc(), customer_id, business_id))
    return {"ok": True, "message": "Customer updated."}


@router.delete("/customers/{customer_id}")
def archive_customer(customer_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "customers")
    if not _same_business(db, "customers", customer_id, business_id):
        raise HTTPException(status_code=404, detail="Customer not found.")
    with transaction(db):
        db.execute("UPDATE customers SET archived=1,updated_at=? WHERE id=? AND business_id=?", (iso_utc(), customer_id, business_id))
    return {"ok": True}


@router.get("/customers/{customer_id}")
def customer_detail(customer_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "customers")
    customer = _same_business(db, "customers", customer_id, business_id)
    if not customer or customer["archived"]:
        raise HTTPException(status_code=404, detail="Customer not found.")
    sales = db.execute("SELECT id,sale_number,total,paid_amount,balance,status,created_at FROM sales WHERE business_id=? AND customer_id=? ORDER BY created_at DESC LIMIT 100",
                       (business_id, customer_id)).fetchall()
    invoices = db.execute("SELECT id,invoice_number,total,paid_amount,balance,status,invoice_date,due_date FROM invoices WHERE business_id=? AND customer_id=? ORDER BY created_at DESC LIMIT 100",
                          (business_id, customer_id)).fetchall()
    total = db.execute("SELECT COALESCE(SUM(total),0) AS amount,COALESCE(SUM(balance),0) AS balance FROM sales WHERE business_id=? AND customer_id=?",
                       (business_id, customer_id)).fetchone()
    return {"customer": dict(customer), "sales": [dict(r) for r in sales], "invoices": [dict(r) for r in invoices],
            "total_purchases": total["amount"], "outstanding_balance": total["balance"]}


@router.get("/suppliers")
def list_suppliers(search: str = "", ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "suppliers")
    require_feature(ctx, "suppliers", "Supplier management is available on Starter and Business plans.")
    sql = "SELECT * FROM suppliers WHERE business_id=? AND archived=0"
    args: list[Any] = [business_id]
    if search.strip():
        like = f"%{search.strip()[:100]}%"
        sql += " AND (lower(name) LIKE lower(?) OR lower(phone) LIKE lower(?) OR lower(email) LIKE lower(?))"
        args.extend([like, like, like])
    sql += " ORDER BY name"
    return {"items": [dict(r) for r in db.execute(sql, tuple(args)).fetchall()]}


@router.get("/suppliers/{supplier_id}")
def supplier_detail(supplier_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "suppliers")
    require_feature(ctx, "suppliers", "Supplier management is available on Starter and Business plans.")
    supplier = _same_business(db, "suppliers", supplier_id, business_id)
    if not supplier or supplier["archived"]:
        raise HTTPException(status_code=404, detail="Supplier not found.")
    products = db.execute("SELECT id,name,sku,quantity,purchase_price,selling_price FROM products WHERE business_id=? AND supplier_id=? AND archived=0 ORDER BY name",
                          (business_id, supplier_id)).fetchall()
    purchases = db.execute("SELECT m.created_at,m.quantity_delta,m.reason,p.name AS product_name FROM inventory_movements m JOIN products p ON p.id=m.product_id "
                           "WHERE m.business_id=? AND p.supplier_id=? AND m.movement_type='PURCHASE' ORDER BY m.created_at DESC LIMIT 100",
                           (business_id, supplier_id)).fetchall()
    return {"supplier": dict(supplier), "products": [dict(r) for r in products], "purchase_history": [dict(r) for r in purchases]}


@router.post("/suppliers")
def create_supplier(payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "suppliers")
    require_feature(ctx, "suppliers", "Supplier management is available on Starter and Business plans.")
    try:
        name = clean_text(payload.get("name"), limit=140, required=True, label="Supplier name")
        contact = clean_text(payload.get("contact_person"), limit=120, label="Contact person")
        phone = clean_text(payload.get("phone"), limit=40, label="Phone")
        email = clean_text(payload.get("email"), limit=254, label="Email")
        address = clean_text(payload.get("address"), limit=500, label="Address")
        notes = clean_text(payload.get("notes"), limit=1000, label="Notes")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    supplier_id, now = make_id(), iso_utc()
    with transaction(db):
        db.execute("INSERT INTO suppliers(id,business_id,name,contact_person,phone,email,address,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (supplier_id, business_id, name, contact, phone, email, address, notes, now, now))
    return {"ok": True, "id": supplier_id, "message": "Supplier added."}


@router.put("/suppliers/{supplier_id}")
def update_supplier(supplier_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "suppliers")
    require_feature(ctx, "suppliers", "Supplier management is available on Starter and Business plans.")
    item = _same_business(db, "suppliers", supplier_id, business_id)
    if not item or item["archived"]:
        raise HTTPException(status_code=404, detail="Supplier not found.")
    try:
        name = clean_text(payload.get("name", item["name"]), limit=140, required=True, label="Supplier name")
        contact = clean_text(payload.get("contact_person", item["contact_person"]), limit=120, label="Contact person")
        phone = clean_text(payload.get("phone", item["phone"]), limit=40, label="Phone")
        email = clean_text(payload.get("email", item["email"]), limit=254, label="Email")
        address = clean_text(payload.get("address", item["address"]), limit=500, label="Address")
        notes = clean_text(payload.get("notes", item["notes"]), limit=1000, label="Notes")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    with transaction(db):
        db.execute("UPDATE suppliers SET name=?,contact_person=?,phone=?,email=?,address=?,notes=?,updated_at=? WHERE id=? AND business_id=?",
                   (name, contact, phone, email, address, notes, iso_utc(), supplier_id, business_id))
    return {"ok": True}


@router.delete("/suppliers/{supplier_id}")
def archive_supplier(supplier_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "suppliers")
    require_feature(ctx, "suppliers", "Supplier management is available on Starter and Business plans.")
    item = _same_business(db, "suppliers", supplier_id, business_id)
    if not item:
        raise HTTPException(status_code=404, detail="Supplier not found.")
    with transaction(db):
        db.execute("UPDATE suppliers SET archived=1,updated_at=? WHERE id=? AND business_id=?", (iso_utc(), supplier_id, business_id))
    return {"ok": True}


@router.get("/settings")
def business_settings(ctx: dict = Depends(get_business_ctx)):
    _require(ctx, "settings")
    return {"business": ctx["business"], "currency_options": ["LKR", "USD", "EUR", "GBP", "INR"]}


@router.put("/settings")
def update_business_settings(payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "settings")
    try:
        name = clean_text(payload.get("name", ctx["business"]["business_name"]), limit=160, required=True, label="Business name")
        business_type = clean_text(payload.get("business_type", ctx["business"]["business_type"]), limit=80, required=True, label="Business type")
        phone = clean_text(payload.get("phone", ctx["business"]["business_phone"]), limit=40, label="Phone")
        email = clean_text(payload.get("email", ctx["business"]["business_email"]), limit=254, label="Email")
        address = clean_text(payload.get("address", ctx["business"]["address"]), limit=500, label="Address")
        currency = clean_text(payload.get("currency", ctx["business"]["currency"]), limit=3, required=True, label="Currency").upper()
        if currency not in {"LKR", "USD", "EUR", "GBP", "INR"}:
            raise ValueError("Choose a supported currency.")
        tax = clean_text(payload.get("tax_details", ctx["business"]["tax_details"]), limit=180, label="Tax details")
        prefix = clean_text(payload.get("invoice_prefix", ctx["business"]["invoice_prefix"]), limit=12, required=True, label="Invoice prefix")
        notes = clean_text(payload.get("invoice_notes", ctx["business"]["invoice_notes"]), limit=1000, label="Invoice notes")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    with transaction(db):
        db.execute("UPDATE businesses SET name=?,business_type=?,phone=?,email=?,address=?,currency=?,tax_details=?,invoice_prefix=?,invoice_notes=?,updated_at=? WHERE id=?",
                   (name, business_type, phone, email, address, currency, tax, prefix, notes, iso_utc(), business_id))
    return {"ok": True, "message": "Business settings saved."}


@router.get("/account")
def account(user: dict = Depends(get_current_user), db=Depends(get_db)):
    row = db.execute("SELECT identity_number_encrypted FROM users WHERE id=?", (user["id"],)).fetchone()
    identity = decrypt_identity_number(row["identity_number_encrypted"]) if row else None
    account_user = public_user(user)
    account_user["identity_number_masked"] = mask_identity_number(identity)
    ctx = None
    if user["role"] != "SUPER_ADMIN":
        ctx = get_business_context(db, user, allow_suspended=True)
    return {"user": account_user, "business": ctx.get("business") if ctx else None}


@router.put("/account")
def update_account(payload: dict = Body(...), user: dict = Depends(get_current_user), db=Depends(get_db)):
    try:
        name = clean_text(payload.get("full_name", user["full_name"]), limit=120, required=True, label="Full name")
        email = clean_email(payload.get("email", user["email"]))
        phone = clean_phone(payload.get("phone", user["phone"]))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    other = db.execute("SELECT id FROM users WHERE lower(email)=lower(?) AND id<>?", (email, user["id"])).fetchone()
    if other:
        raise HTTPException(status_code=409, detail="That email is already in use.")
    with transaction(db):
        db.execute("UPDATE users SET full_name=?,email=?,phone=?,updated_at=? WHERE id=?", (name, email, phone, iso_utc(), user["id"]))
    return {"ok": True, "message": "Account details updated."}


@router.get("/staff")
def list_staff(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "staff")
    rows = db.execute("SELECT bm.id AS membership_id,bm.role,bm.joined_at,u.id AS user_id,u.public_account_id,u.full_name,u.username,u.email,u.phone,u.account_status,COALESCE(u.last_login_at,u.last_login) AS last_login,u.must_change_password " 
                      "FROM business_members bm JOIN users u ON u.id=bm.user_id WHERE bm.business_id=? ORDER BY CASE bm.role WHEN 'OWNER' THEN 0 WHEN 'MANAGER' THEN 1 ELSE 2 END,u.full_name",
                      (business_id,)).fetchall()
    return {"items": [dict(r) for r in rows], "limit": ctx["plan"]["staff_limit"], "plan": ctx["effective_plan_id"]}


@router.post("/staff")
def create_staff(payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "staff")
    try:
        full_name = clean_text(payload.get("full_name"), limit=120, required=True, label="Full name")
        identity_number = normalize_identity_number(payload.get("identity_number"))
        email = clean_email(payload.get("email"))
        phone = clean_phone(payload.get("phone"))
        role = clean_text(payload.get("role", "STAFF"), limit=20).upper()
        if role not in {"STAFF", "MANAGER"}:
            raise ValueError("Choose Staff or Manager.")
        password = clean_text(payload.get("temporary_password") or new_secret(), limit=128, required=True, label="Temporary password")
        password_hash = hash_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    identity_hash = identity_lookup_hash(identity_number)
    limit = int(ctx["plan"]["staff_limit"])
    count = db.execute("SELECT COUNT(*) AS n FROM business_members WHERE business_id=?", (business_id,)).fetchone()["n"]
    if limit >= 0 and count >= limit:
        raise HTTPException(status_code=402, detail=f"Your {ctx['effective_plan_id'].title()} plan allows {limit} team member(s). Upgrade your plan to add more.", headers={"X-BizFlow-Error": "PLAN_LIMIT"})
    if db.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    if db.execute("SELECT id FROM users WHERE identity_number_lookup_hash=?", (identity_hash,)).fetchone():
        raise HTTPException(status_code=409, detail="This identity number is already associated with an account.")
    user_id, now = make_id(), iso_utc()
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
                (user_id, public_account_id, full_name, username, encrypt_identity_number(identity_number), identity_hash,
                 email, phone, password_hash, now, now, now),
            )
            db.execute("INSERT INTO business_members(id,business_id,user_id,role,joined_at) VALUES(?,?,?,?,?)",
                       (make_id(), business_id, user_id, role, now))
    except Exception as exc:
        if isinstance(exc, sqlite3.IntegrityError) or getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="An account could not be created because an email or identity number is already in use.") from None
        raise
    return {"ok": True, "id": user_id, "username": username, "public_account_id": public_account_id,
            "temporary_password": password, "message": "Team member created. Share the temporary password securely; they will be asked to change it at first sign-in."}


@router.put("/staff/{member_id}")
def update_staff(member_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "staff")
    member = db.execute("SELECT * FROM business_members WHERE id=? AND business_id=?", (member_id, business_id)).fetchone()
    if not member or member["role"] == "OWNER":
        raise HTTPException(status_code=404, detail="Team member not found.")
    role = clean_text(payload.get("role", member["role"]), limit=20).upper()
    if role not in {"MANAGER", "STAFF"}:
        raise HTTPException(status_code=422, detail="Choose Staff or Manager.")
    with transaction(db):
        db.execute("UPDATE business_members SET role=? WHERE id=? AND business_id=?", (role, member_id, business_id))
    return {"ok": True, "message": "Team role updated."}


@router.delete("/staff/{member_id}")
def remove_staff(member_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "staff")
    member = db.execute("SELECT * FROM business_members WHERE id=? AND business_id=?", (member_id, business_id)).fetchone()
    if not member or member["role"] == "OWNER":
        raise HTTPException(status_code=404, detail="Team member not found.")
    with transaction(db):
        db.execute("DELETE FROM business_members WHERE id=? AND business_id=?", (member_id, business_id))
    return {"ok": True}


@router.get("/notifications")
def notifications(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    _bid(ctx)
    rows = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (ctx["user"]["id"],)).fetchall()
    return {"items": [dict(r) for r in rows], "unread": sum(1 for row in rows if not row["read_at"])}


@router.post("/notifications/read-all")
def mark_notifications_read(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _bid(ctx)
    with transaction(db):
        db.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL AND (business_id=? OR business_id IS NULL)",
                   (iso_utc(), ctx["user"]["id"], business_id))
    return {"ok": True}


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _bid(ctx)
    with transaction(db):
        updated = db.execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=? AND (business_id=? OR business_id IS NULL)",
                             (iso_utc(), notification_id, ctx["user"]["id"], business_id))
        if updated.rowcount == 0:
            raise HTTPException(status_code=404, detail="Notification not found.")
    return {"ok": True}


@router.get("/locations")
def list_locations(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "settings")
    require_feature(ctx, "multiple_locations", "Multiple business locations are available on the Business plan.")
    rows = db.execute("SELECT * FROM locations WHERE business_id=? ORDER BY name", (business_id,)).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/locations")
def create_location(payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "settings")
    require_feature(ctx, "multiple_locations", "Multiple business locations are available on the Business plan.")
    try:
        name = clean_text(payload.get("name"), limit=100, required=True, label="Location name")
        address = clean_text(payload.get("address"), limit=400, label="Address")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    location_id, now = make_id(), iso_utc()
    if db.execute("SELECT id FROM locations WHERE business_id=? AND lower(name)=lower(?)", (business_id, name)).fetchone():
        raise HTTPException(status_code=409, detail="A location with this name already exists.")
    with transaction(db):
        db.execute("INSERT INTO locations(id,business_id,name,address,created_at) VALUES(?,?,?,?,?)", (location_id, business_id, name, address, now))
    return {"ok": True, "id": location_id}


@router.put("/locations/{location_id}")
def update_location(location_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "settings")
    require_feature(ctx, "multiple_locations", "Multiple business locations are available on the Business plan.")
    try:
        name = clean_text(payload.get("name"), limit=100, required=True, label="Location name")
        address = clean_text(payload.get("address", ""), limit=400, label="Address")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not _same_business(db, "locations", location_id, business_id):
        raise HTTPException(status_code=404, detail="Location not found.")
    duplicate = db.execute("SELECT id FROM locations WHERE business_id=? AND lower(name)=lower(?) AND id<>?", (business_id, name, location_id)).fetchone()
    if duplicate:
        raise HTTPException(status_code=409, detail="A location with this name already exists.")
    with transaction(db):
        db.execute("UPDATE locations SET name=?,address=? WHERE id=? AND business_id=?", (name, address, location_id, business_id))
    return {"ok": True}


@router.delete("/locations/{location_id}")
def delete_location(location_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "settings")
    require_feature(ctx, "multiple_locations", "Multiple business locations are available on the Business plan.")
    with transaction(db):
        deleted = db.execute("DELETE FROM locations WHERE id=? AND business_id=?", (location_id, business_id))
        if deleted.rowcount == 0:
            raise HTTPException(status_code=404, detail="Location not found.")
    return {"ok": True}
