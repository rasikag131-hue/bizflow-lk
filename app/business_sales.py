from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.business_base import _bid, _customer_id, _require, _same_business
from app.config import settings
from app.db import get_db, transaction
from app.deps import get_business_ctx
from app.security import clean_money, clean_quantity, clean_text, iso_utc, make_id
from app.services import require_feature
from app.storage import verify_raster_image, write_private_file
from app.timeutils import local_today, utc_bounds

router = APIRouter(prefix="/api", tags=["sales and documents"])
PAYMENT_METHODS = {"Cash", "Bank Transfer", "Card", "Other"}


def _invoice_status(total: int, paid: int, due_date: str | None) -> str:
    balance = max(0, total - paid)
    if balance == 0:
        return "PAID"
    if due_date and due_date < local_today().isoformat():
        return "OVERDUE"
    if paid > 0:
        return "PARTIALLY_PAID"
    return "UNPAID"


def _document_lines(db: Any, business_id: str, raw_items: Any, *, use_catalog_price: bool = False,
                    require_stock: bool = False) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 100:
        raise HTTPException(status_code=422, detail="Add between 1 and 100 line items.")
    lines: list[dict[str, Any]] = []
    subtotal = 0
    requested_stock: dict[str, float] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="Each item must be a valid line item.")
        try:
            quantity = clean_quantity(raw.get("quantity"), label="Quantity", allow_zero=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        product_id = clean_text(raw.get("product_id"), limit=80, label="Product ID") or None
        product = None
        if product_id:
            product = _same_business(db, "products", product_id, business_id)
            if not product or product["archived"]:
                raise HTTPException(status_code=422, detail="A line item refers to a product outside this business.")
            product_name = product["name"]
            unit_price = int(product["selling_price"]) if use_catalog_price else clean_money(raw.get("unit_price", product["selling_price"]), label="Unit price")
            unit_cost = int(product["purchase_price"])
            if require_stock:
                requested_stock[product_id] = requested_stock.get(product_id, 0) + quantity
        else:
            if require_stock:
                raise HTTPException(status_code=422, detail="Choose a product from your inventory for a POS sale.")
            try:
                product_name = clean_text(raw.get("product_name") or raw.get("name"), limit=160, required=True, label="Item name")
                unit_price = clean_money(raw.get("unit_price"), label="Unit price")
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            unit_cost = 0
        line_total = int(round(quantity * unit_price))
        subtotal += line_total
        lines.append({"product_id": product_id, "product_name": product_name, "quantity": quantity,
                      "unit_price": unit_price, "unit_cost": unit_cost, "line_total": line_total})
    if require_stock:
        lock_suffix = " FOR UPDATE" if getattr(db, "dialect", "sqlite") == "postgres" else ""
        for product_id, needed in requested_stock.items():
            product = db.execute(f"SELECT quantity FROM products WHERE id=? AND business_id=? AND archived=0{lock_suffix}",
                                 (product_id, business_id)).fetchone()
            if not product or float(product["quantity"]) < needed:
                name = next((line["product_name"] for line in lines if line["product_id"] == product_id), "Product")
                raise HTTPException(status_code=409, detail=f"Not enough stock for {name}. Available: {product['quantity'] if product else 0}.")
    return lines, subtotal


def _check_invoice_limit(db: Any, ctx: dict[str, Any], business_id: str) -> None:
    limit = int(ctx["plan"]["invoice_limit"])
    if limit < 0:
        return
    today = local_today()
    start_date = today.replace(day=1)
    next_month = (start_date.replace(day=28) + timedelta(days=4)).replace(day=1)
    start, end = utc_bounds(start_date, next_month)
    count = db.execute("SELECT COUNT(*) AS n FROM invoices WHERE business_id=? AND created_at>=? AND created_at<?", (business_id, start, end)).fetchone()["n"]
    if count >= limit:
        raise HTTPException(status_code=402, detail=f"Your Free plan allows up to {limit} invoices per month. Upgrade your plan to create more.", headers={"X-BizFlow-Error": "PLAN_LIMIT"})


def _doc_number(prefix: str, kind: str) -> str:
    day = local_today().strftime("%Y%m%d")
    return f"{prefix}-{day}-{kind}{make_id()[:5].upper()}"


def _check_method(value: Any) -> str:
    method = clean_text(value or "Cash", limit=30, label="Payment method")
    if method not in PAYMENT_METHODS:
        raise HTTPException(status_code=422, detail="Choose a supported payment method.")
    return method


@router.get("/sales")
def list_sales(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "sales")
    rows = db.execute(
        "SELECT s.*,COALESCE(c.name,'Walk-in customer') AS customer_name FROM sales s LEFT JOIN customers c ON c.id=s.customer_id "
        "WHERE s.business_id=? ORDER BY s.created_at DESC LIMIT 500", (business_id,),
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/sales")
def create_sale(payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "sales")
    try:
        discount = clean_money(payload.get("discount", 0), label="Discount")
        customer_id = _customer_id(db, business_id, payload.get("customer_id"))
        method = _check_method(payload.get("payment_method"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    now, sale_id = iso_utc(), make_id()
    with transaction(db):
        lines, subtotal = _document_lines(db, business_id, payload.get("items"), use_catalog_price=True, require_stock=True)
        if discount > subtotal:
            raise HTTPException(status_code=422, detail="Discount cannot be greater than the subtotal.")
        total = subtotal - discount
        if total <= 0:
            raise HTTPException(status_code=422, detail="Sale total must be greater than zero.")
        try:
            paid = clean_money(payload.get("paid_amount", total), label="Paid amount")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if paid > total:
            raise HTTPException(status_code=422, detail="Paid amount cannot be greater than the sale total.")
        balance = total - paid
        status = "PAID" if balance == 0 else ("PARTIALLY_PAID" if paid > 0 else "UNPAID")
        sale_number = _doc_number("SALE", "")
        db.execute("INSERT INTO sales(id,business_id,sale_number,customer_id,subtotal,discount,total,paid_amount,balance,payment_method,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sale_id, business_id, sale_number, customer_id, subtotal, discount, total, paid, balance, method, status, ctx["user"]["id"], now))
        for line in lines:
            db.execute("INSERT INTO sale_items(id,business_id,sale_id,product_id,product_name,quantity,unit_price,unit_cost,line_total) VALUES(?,?,?,?,?,?,?,?,?)",
                       (make_id(), business_id, sale_id, line["product_id"], line["product_name"], line["quantity"], line["unit_price"], line["unit_cost"], line["line_total"]))
            product = db.execute("SELECT quantity FROM products WHERE id=? AND business_id=?", (line["product_id"], business_id)).fetchone()
            previous, new_quantity = float(product["quantity"]), float(product["quantity"]) - line["quantity"]
            db.execute("UPDATE products SET quantity=?,updated_at=? WHERE id=? AND business_id=?", (new_quantity, now, line["product_id"], business_id))
            db.execute("INSERT INTO inventory_movements(id,business_id,product_id,movement_type,quantity_delta,previous_quantity,new_quantity,reason,user_id,created_at) VALUES(?,?,?,'SALE',?,?,?,?,?,?)",
                       (make_id(), business_id, line["product_id"], -line["quantity"], previous, new_quantity, f"Sale {sale_number}", ctx["user"]["id"], now))
        if paid:
            db.execute("INSERT INTO payments(id,business_id,user_id,sale_id,amount,payment_method,status,paid_at,created_at) VALUES(?,?,?,?,?,?,'APPROVED',?,?)",
                       (make_id(), business_id, ctx["user"]["id"], sale_id, paid, method, now, now))
    return {"ok": True, "id": sale_id, "sale_number": sale_number, "total": total, "paid_amount": paid, "balance": balance, "status": status}


@router.get("/sales/{sale_id}")
def sale_detail(sale_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "sales")
    sale = _same_business(db, "sales", sale_id, business_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found.")
    items = db.execute("SELECT * FROM sale_items WHERE sale_id=? AND business_id=?", (sale_id, business_id)).fetchall()
    return {"sale": dict(sale), "items": [dict(r) for r in items]}


@router.post("/sales/{sale_id}/payments")
def add_sale_payment(sale_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "sales")
    try:
        amount = clean_money(payload.get("amount"), label="Payment amount", allow_zero=False)
        method = _check_method(payload.get("payment_method"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    now = iso_utc()
    with transaction(db):
        lock_suffix = " FOR UPDATE" if getattr(db, "dialect", "sqlite") == "postgres" else ""
        sale = db.execute(f"SELECT * FROM sales WHERE id=? AND business_id=?{lock_suffix}", (sale_id, business_id)).fetchone()
        if not sale:
            raise HTTPException(status_code=404, detail="Sale not found.")
        if amount > int(sale["balance"]):
            raise HTTPException(status_code=422, detail="Payment cannot be greater than the outstanding balance.")
        paid = int(sale["paid_amount"]) + amount
        balance = int(sale["total"]) - paid
        status = "PAID" if balance == 0 else "PARTIALLY_PAID"
        db.execute("UPDATE sales SET paid_amount=?,balance=?,status=? WHERE id=? AND business_id=?", (paid, balance, status, sale_id, business_id))
        db.execute("INSERT INTO payments(id,business_id,user_id,sale_id,amount,payment_method,status,paid_at,created_at) VALUES(?,?,?,?,?,?,'APPROVED',?,?)",
                   (make_id(), business_id, ctx["user"]["id"], sale_id, amount, method, now, now))
    return {"ok": True, "paid_amount": paid, "balance": balance, "status": status}


@router.get("/invoices")
def list_invoices(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "invoices")
    rows = db.execute("SELECT i.*,COALESCE(c.name,'Unspecified customer') AS customer_name FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id WHERE i.business_id=? ORDER BY i.created_at DESC LIMIT 500",
                      (business_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["status"] = _invoice_status(int(item["total"]), int(item["paid_amount"]), item["due_date"])
        result.append(item)
    return {"items": result, "monthly_limit": ctx["plan"]["invoice_limit"]}


@router.post("/invoices")
def create_invoice(payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "invoices")
    require_feature(ctx, "invoices", "Invoices are available on the Free plan within its monthly limit.")
    try:
        discount = clean_money(payload.get("discount", 0), label="Discount")
        customer_id = _customer_id(db, business_id, payload.get("customer_id"))
        invoice_date = date.fromisoformat(clean_text(payload.get("invoice_date") or local_today().isoformat(), limit=10, label="Invoice date")).isoformat()
        due_date = payload.get("due_date") or None
        if due_date:
            due_date = date.fromisoformat(clean_text(due_date, limit=10, label="Due date")).isoformat()
        notes = clean_text(payload.get("notes", ""), limit=1200, label="Notes")
        method = _check_method(payload.get("payment_method"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc) or "Choose valid invoice dates.") from None
    now, invoice_id = iso_utc(), make_id()
    with transaction(db):
        _check_invoice_limit(db, ctx, business_id)
        lines, subtotal = _document_lines(db, business_id, payload.get("items"))
        if discount > subtotal:
            raise HTTPException(status_code=422, detail="Discount cannot be greater than the subtotal.")
        total = subtotal - discount
        if total <= 0:
            raise HTTPException(status_code=422, detail="Invoice total must be greater than zero.")
        try:
            paid = clean_money(payload.get("paid_amount", 0), label="Paid amount")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if paid > total:
            raise HTTPException(status_code=422, detail="Paid amount cannot be greater than the invoice total.")
        balance = total - paid
        status = _invoice_status(total, paid, due_date)
        business = ctx["business"]
        number = _doc_number(clean_text(business["invoice_prefix"] or "INV", limit=12), "")
        db.execute("INSERT INTO invoices(id,business_id,invoice_number,customer_id,status,subtotal,discount,total,paid_amount,balance,invoice_date,due_date,notes,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (invoice_id, business_id, number, customer_id, status, subtotal, discount, total, paid, balance, invoice_date, due_date, notes, ctx["user"]["id"], now, now))
        for line in lines:
            db.execute("INSERT INTO invoice_items(id,business_id,invoice_id,product_id,product_name,quantity,unit_price,line_total) VALUES(?,?,?,?,?,?,?,?)",
                       (make_id(), business_id, invoice_id, line["product_id"], line["product_name"], line["quantity"], line["unit_price"], line["line_total"]))
        if paid:
            db.execute("INSERT INTO payments(id,business_id,user_id,invoice_id,amount,payment_method,status,paid_at,created_at) VALUES(?,?,?,?,?,?,'APPROVED',?,?)",
                       (make_id(), business_id, ctx["user"]["id"], invoice_id, paid, method, now, now))
    return {"ok": True, "id": invoice_id, "invoice_number": number, "total": total, "status": status}


@router.get("/invoices/{invoice_id}")
def invoice_detail(invoice_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "invoices")
    invoice = db.execute("SELECT i.*,COALESCE(c.name,'Unspecified customer') AS customer_name,c.phone AS customer_phone,c.email AS customer_email "
                         "FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id WHERE i.id=? AND i.business_id=?", (invoice_id, business_id)).fetchone()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=? AND business_id=?", (invoice_id, business_id)).fetchall()
    payments = db.execute("SELECT amount,payment_method,paid_at FROM payments WHERE invoice_id=? AND business_id=? ORDER BY paid_at", (invoice_id, business_id)).fetchall()
    result = dict(invoice)
    result["status"] = _invoice_status(int(invoice["total"]), int(invoice["paid_amount"]), invoice["due_date"])
    return {"invoice": result, "items": [dict(r) for r in items], "payments": [dict(r) for r in payments]}


@router.put("/invoices/{invoice_id}")
def update_invoice(invoice_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "invoices")
    existing = _same_business(db, "invoices", invoice_id, business_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    if int(existing["paid_amount"]) > 0:
        raise HTTPException(status_code=409, detail="An invoice with recorded payments cannot be edited. Record a payment or create a credit adjustment instead.")
    try:
        discount = clean_money(payload.get("discount", existing["discount"]), label="Discount")
        customer_id = _customer_id(db, business_id, payload.get("customer_id", existing["customer_id"]))
        invoice_date = date.fromisoformat(clean_text(payload.get("invoice_date", existing["invoice_date"]), limit=10, label="Invoice date")).isoformat()
        due_date = payload.get("due_date", existing["due_date"]) or None
        if due_date:
            due_date = date.fromisoformat(clean_text(due_date, limit=10, label="Due date")).isoformat()
        notes = clean_text(payload.get("notes", existing["notes"]), limit=1200, label="Notes")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc) or "Choose valid invoice dates.") from None
    now = iso_utc()
    with transaction(db):
        lines, subtotal = _document_lines(db, business_id, payload.get("items"))
        if discount > subtotal:
            raise HTTPException(status_code=422, detail="Discount cannot be greater than the subtotal.")
        total = subtotal - discount
        if total <= 0:
            raise HTTPException(status_code=422, detail="Invoice total must be greater than zero.")
        status = _invoice_status(total, 0, due_date)
        db.execute("UPDATE invoices SET customer_id=?,status=?,subtotal=?,discount=?,total=?,paid_amount=0,balance=?,invoice_date=?,due_date=?,notes=?,updated_at=? WHERE id=? AND business_id=?",
                   (customer_id, status, subtotal, discount, total, total, invoice_date, due_date, notes, now, invoice_id, business_id))
        db.execute("DELETE FROM invoice_items WHERE invoice_id=? AND business_id=?", (invoice_id, business_id))
        for line in lines:
            db.execute("INSERT INTO invoice_items(id,business_id,invoice_id,product_id,product_name,quantity,unit_price,line_total) VALUES(?,?,?,?,?,?,?,?)",
                       (make_id(), business_id, invoice_id, line["product_id"], line["product_name"], line["quantity"], line["unit_price"], line["line_total"]))
    return {"ok": True, "message": "Invoice updated."}


@router.post("/invoices/{invoice_id}/payments")
def add_invoice_payment(invoice_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "invoices")
    try:
        amount = clean_money(payload.get("amount"), label="Payment amount", allow_zero=False)
        method = _check_method(payload.get("payment_method"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    now = iso_utc()
    with transaction(db):
        lock_suffix = " FOR UPDATE" if getattr(db, "dialect", "sqlite") == "postgres" else ""
        invoice = db.execute(f"SELECT * FROM invoices WHERE id=? AND business_id=?{lock_suffix}", (invoice_id, business_id)).fetchone()
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        if amount > int(invoice["balance"]):
            raise HTTPException(status_code=422, detail="Payment cannot be greater than the outstanding balance.")
        paid = int(invoice["paid_amount"]) + amount
        balance = int(invoice["total"]) - paid
        status = _invoice_status(int(invoice["total"]), paid, invoice["due_date"])
        db.execute("UPDATE invoices SET paid_amount=?,balance=?,status=?,updated_at=? WHERE id=? AND business_id=?",
                   (paid, balance, status, now, invoice_id, business_id))
        db.execute("INSERT INTO payments(id,business_id,user_id,invoice_id,amount,payment_method,status,paid_at,created_at) VALUES(?,?,?,?,?,?,'APPROVED',?,?)",
                   (make_id(), business_id, ctx["user"]["id"], invoice_id, amount, method, now, now))
    return {"ok": True, "paid_amount": paid, "balance": balance, "status": status}


@router.get("/quotations")
def list_quotations(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "quotations")
    require_feature(ctx, "quotations", "Quotations are available on Starter and Business plans.")
    rows = db.execute("SELECT q.*,COALESCE(c.name,'Unspecified customer') AS customer_name FROM quotations q LEFT JOIN customers c ON c.id=q.customer_id WHERE q.business_id=? ORDER BY q.created_at DESC LIMIT 500",
                      (business_id,)).fetchall()
    result = []
    today = local_today().isoformat()
    for row in rows:
        item = dict(row)
        if item["valid_until"] and item["valid_until"] < today and item["status"] not in {"ACCEPTED", "REJECTED"}:
            item["status"] = "EXPIRED"
        result.append(item)
    return {"items": result}


@router.post("/quotations")
def create_quotation(payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "quotations")
    require_feature(ctx, "quotations", "Quotations are available on Starter and Business plans.")
    try:
        discount = clean_money(payload.get("discount", 0), label="Discount")
        customer_id = _customer_id(db, business_id, payload.get("customer_id"))
        valid_until = payload.get("valid_until") or None
        if valid_until:
            valid_until = date.fromisoformat(clean_text(valid_until, limit=10, label="Valid until")).isoformat()
        notes = clean_text(payload.get("notes", ""), limit=1200, label="Notes")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc) or "Choose a valid validity date.") from None
    now, quote_id = iso_utc(), make_id()
    with transaction(db):
        lines, subtotal = _document_lines(db, business_id, payload.get("items"))
        if discount > subtotal:
            raise HTTPException(status_code=422, detail="Discount cannot be greater than the subtotal.")
        total = subtotal - discount
        if total <= 0:
            raise HTTPException(status_code=422, detail="Quotation total must be greater than zero.")
        number = _doc_number("QUO", "")
        db.execute("INSERT INTO quotations(id,business_id,quotation_number,customer_id,status,subtotal,discount,total,valid_until,notes,created_by,created_at,updated_at) VALUES(?,?,?,?,'DRAFT',?,?,?,?,?,?,?,?)",
                   (quote_id, business_id, number, customer_id, subtotal, discount, total, valid_until, notes, ctx["user"]["id"], now, now))
        for line in lines:
            db.execute("INSERT INTO quotation_items(id,business_id,quotation_id,product_id,product_name,quantity,unit_price,line_total) VALUES(?,?,?,?,?,?,?,?)",
                       (make_id(), business_id, quote_id, line["product_id"], line["product_name"], line["quantity"], line["unit_price"], line["line_total"]))
    return {"ok": True, "id": quote_id, "quotation_number": number}


@router.put("/quotations/{quotation_id}")
def update_quotation(quotation_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "quotations")
    require_feature(ctx, "quotations", "Quotations are available on Starter and Business plans.")
    existing = db.execute("SELECT * FROM quotations WHERE id=? AND business_id=?", (quotation_id, business_id)).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Quotation not found.")
    if existing["status"] not in {"DRAFT", "SENT"} or (existing["valid_until"] and existing["valid_until"] < local_today().isoformat()):
        raise HTTPException(status_code=409, detail="Only active draft or sent quotations can be edited.")
    try:
        discount = clean_money(payload.get("discount", existing["discount"]), label="Discount")
        customer_id = _customer_id(db, business_id, payload.get("customer_id", existing["customer_id"]))
        valid_until = payload.get("valid_until", existing["valid_until"]) or None
        if valid_until:
            valid_until = date.fromisoformat(clean_text(valid_until, limit=10, label="Valid until")).isoformat()
        notes = clean_text(payload.get("notes", existing["notes"]), limit=1200, label="Notes")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc) or "Choose a valid validity date.") from None
    now = iso_utc()
    with transaction(db):
        quote = db.execute("SELECT * FROM quotations WHERE id=? AND business_id=?", (quotation_id, business_id)).fetchone()
        if not quote or quote["status"] not in {"DRAFT", "SENT"} or (quote["valid_until"] and quote["valid_until"] < local_today().isoformat()):
            raise HTTPException(status_code=409, detail="This quotation can no longer be edited.")
        lines, subtotal = _document_lines(db, business_id, payload.get("items"))
        if discount > subtotal:
            raise HTTPException(status_code=422, detail="Discount cannot be greater than the subtotal.")
        total = subtotal - discount
        if total <= 0:
            raise HTTPException(status_code=422, detail="Quotation total must be greater than zero.")
        db.execute("UPDATE quotations SET customer_id=?,subtotal=?,discount=?,total=?,valid_until=?,notes=?,updated_at=? WHERE id=? AND business_id=?",
                   (customer_id, subtotal, discount, total, valid_until, notes, now, quotation_id, business_id))
        db.execute("DELETE FROM quotation_items WHERE quotation_id=? AND business_id=?", (quotation_id, business_id))
        for line in lines:
            db.execute("INSERT INTO quotation_items(id,business_id,quotation_id,product_id,product_name,quantity,unit_price,line_total) VALUES(?,?,?,?,?,?,?,?)",
                       (make_id(), business_id, quotation_id, line["product_id"], line["product_name"], line["quantity"], line["unit_price"], line["line_total"]))
    return {"ok": True, "message": "Quotation updated."}


@router.get("/quotations/{quotation_id}")
def quotation_detail(quotation_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "quotations")
    require_feature(ctx, "quotations", "Quotations are available on Starter and Business plans.")
    quote = db.execute("SELECT q.*,COALESCE(c.name,'Unspecified customer') AS customer_name FROM quotations q LEFT JOIN customers c ON c.id=q.customer_id WHERE q.id=? AND q.business_id=?", (quotation_id, business_id)).fetchone()
    if not quote:
        raise HTTPException(status_code=404, detail="Quotation not found.")
    items = db.execute("SELECT * FROM quotation_items WHERE quotation_id=? AND business_id=?", (quotation_id, business_id)).fetchall()
    result = dict(quote)
    if result["valid_until"] and result["valid_until"] < local_today().isoformat() and result["status"] not in {"ACCEPTED", "REJECTED"}:
        result["status"] = "EXPIRED"
    return {"quotation": result, "items": [dict(r) for r in items]}


@router.post("/quotations/{quotation_id}/status")
def set_quotation_status(quotation_id: str, payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "quotations")
    require_feature(ctx, "quotations", "Quotations are available on Starter and Business plans.")
    status = clean_text(payload.get("status"), limit=20).upper()
    if status not in {"DRAFT", "SENT", "ACCEPTED", "REJECTED"}:
        raise HTTPException(status_code=422, detail="Choose Draft, Sent, Accepted, or Rejected.")
    with transaction(db):
        quote = db.execute("SELECT * FROM quotations WHERE id=? AND business_id=?", (quotation_id, business_id)).fetchone()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found.")
        if quote["status"] == "ACCEPTED" and status != "ACCEPTED":
            raise HTTPException(status_code=409, detail="An accepted quotation cannot be moved back to another status.")
        db.execute("UPDATE quotations SET status=?,updated_at=? WHERE id=? AND business_id=?", (status, iso_utc(), quotation_id, business_id))
    return {"ok": True, "status": status}


@router.post("/quotations/{quotation_id}/convert")
def convert_quotation(quotation_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "quotations")
    require_feature(ctx, "quotations", "Quotations are available on Starter and Business plans.")
    require_feature(ctx, "invoices", "Invoices are not available on your current plan.")
    with transaction(db):
        quote = db.execute("SELECT * FROM quotations WHERE id=? AND business_id=?", (quotation_id, business_id)).fetchone()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found.")
        existing = db.execute("SELECT id,invoice_number FROM invoices WHERE source_quotation_id=? AND business_id=?", (quotation_id, business_id)).fetchone()
        if existing:
            return {"ok": True, "id": existing["id"], "invoice_number": existing["invoice_number"], "already_converted": True}
        if quote["valid_until"] and quote["valid_until"] < local_today().isoformat():
            raise HTTPException(status_code=409, detail="This quotation has expired. Create a new quotation before converting it.")
        _check_invoice_limit(db, ctx, business_id)
        invoice_id, now = make_id(), iso_utc()
        business = ctx["business"]
        number = _doc_number(clean_text(business["invoice_prefix"] or "INV", limit=12), "")
        status = _invoice_status(int(quote["total"]), 0, None)
        db.execute("INSERT INTO invoices(id,business_id,invoice_number,customer_id,status,subtotal,discount,total,paid_amount,balance,invoice_date,due_date,notes,source_quotation_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,0,?,?,NULL,?,?,?,?,?)",
                   (invoice_id, business_id, number, quote["customer_id"], status, quote["subtotal"], quote["discount"], quote["total"], quote["total"], local_today().isoformat(), quote["notes"], quotation_id, ctx["user"]["id"], now, now))
        items = db.execute("SELECT * FROM quotation_items WHERE quotation_id=? AND business_id=?", (quotation_id, business_id)).fetchall()
        for item in items:
            db.execute("INSERT INTO invoice_items(id,business_id,invoice_id,product_id,product_name,quantity,unit_price,line_total) VALUES(?,?,?,?,?,?,?,?)",
                       (make_id(), business_id, invoice_id, item["product_id"], item["product_name"], item["quantity"], item["unit_price"], item["line_total"]))
        db.execute("UPDATE quotations SET status='ACCEPTED',updated_at=? WHERE id=? AND business_id=?", (now, quotation_id, business_id))
    return {"ok": True, "id": invoice_id, "invoice_number": number, "already_converted": False}


@router.get("/expenses")
def list_expenses(ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "expenses")
    require_feature(ctx, "expense_tracking", "Expense tracking is available on Starter and Business plans.")
    rows = db.execute("SELECT * FROM expenses WHERE business_id=? ORDER BY expense_date DESC,created_at DESC LIMIT 1000", (business_id,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        has_receipt = bool(item.pop("receipt_path", ""))
        item["receipt_url"] = f"/api/expenses/{item['id']}/receipt" if has_receipt else ""
        items.append(item)
    return {"items": items}


@router.post("/expenses")
def create_expense(payload: dict = Body(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "expenses")
    require_feature(ctx, "expense_tracking", "Expense tracking is available on Starter and Business plans.")
    try:
        category = clean_text(payload.get("category"), limit=50, required=True, label="Category")
        amount = clean_money(payload.get("amount"), label="Amount", allow_zero=False)
        expense_date = date.fromisoformat(clean_text(payload.get("expense_date") or local_today().isoformat(), limit=10, label="Date")).isoformat()
        description = clean_text(payload.get("description", ""), limit=500, label="Description")
        method = _check_method(payload.get("payment_method"))
        if category not in {"Rent", "Utilities", "Salaries", "Transport", "Marketing", "Inventory", "Maintenance", "Other"}:
            raise ValueError("Choose a valid expense category.")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    expense_id, now = make_id(), iso_utc()
    with transaction(db):
        db.execute("INSERT INTO expenses(id,business_id,category,amount,expense_date,description,payment_method,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (expense_id, business_id, category, amount, expense_date, description, method, ctx["user"]["id"], now))
    return {"ok": True, "id": expense_id}


@router.post("/expenses/{expense_id}/receipt")
async def upload_expense_receipt(expense_id: str, receipt: UploadFile = File(...), ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "expenses")
    require_feature(ctx, "expense_tracking", "Expense tracking is available on Starter and Business plans.")
    expense = db.execute("SELECT * FROM expenses WHERE id=? AND business_id=?", (expense_id, business_id)).fetchone()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found.")
    filename = (receipt.filename or "").replace("\\", "/").split("/")[-1]
    extension = Path(filename).suffix.lower()
    allowed = {".jpg": ("image/jpeg", b"\xff\xd8\xff"), ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
               ".png": ("image/png", b"\x89PNG\r\n\x1a\n"), ".pdf": ("application/pdf", b"%PDF-")}
    if extension not in allowed or receipt.content_type != allowed[extension][0]:
        raise HTTPException(status_code=415, detail="Upload a JPG, JPEG, PNG, or PDF receipt.")
    content = await receipt.read(settings.max_receipt_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_receipt_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Receipt must be {settings.max_receipt_mb} MB or smaller.")
    if not content.startswith(allowed[extension][1]) or (extension == ".pdf" and b"%%EOF" not in content[-2048:]):
        raise HTTPException(status_code=415, detail="The uploaded receipt could not be verified.")
    if extension != ".pdf" and not verify_raster_image(content, extension):
        raise HTTPException(status_code=415, detail="The uploaded image could not be decoded as a valid JPG or PNG.")
    directory = settings.storage_dir / "expenses"
    path = directory / f"{business_id}-{expense_id}-{make_id()}{extension}"
    write_private_file(path, content)
    try:
        with transaction(db):
            db.execute("UPDATE expenses SET receipt_path=? WHERE id=? AND business_id=?", (str(path), expense_id, business_id))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if expense["receipt_path"]:
        try:
            old = Path(expense["receipt_path"]).resolve()
            if settings.storage_dir.resolve() in old.parents:
                old.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "receipt_url": f"/api/expenses/{expense_id}/receipt"}


@router.get("/expenses/{expense_id}/receipt")
def view_expense_receipt(expense_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "expenses")
    require_feature(ctx, "expense_tracking", "Expense tracking is available on Starter and Business plans.")
    expense = db.execute("SELECT receipt_path FROM expenses WHERE id=? AND business_id=?", (expense_id, business_id)).fetchone()
    if not expense or not expense["receipt_path"]:
        raise HTTPException(status_code=404, detail="Expense receipt not found.")
    path = Path(expense["receipt_path"])
    if not path.is_file() or settings.storage_dir.resolve() not in path.resolve().parents:
        raise HTTPException(status_code=404, detail="Expense receipt is unavailable.")
    mime = "application/pdf" if path.suffix.lower() == ".pdf" else "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(path, media_type=mime, headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name)}",
                        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox",
                        "Cache-Control": "private, no-store"})


@router.delete("/expenses/{expense_id}")
def delete_expense(expense_id: str, ctx: dict = Depends(get_business_ctx), db=Depends(get_db)):
    business_id = _require(ctx, "expenses")
    require_feature(ctx, "expense_tracking", "Expense tracking is available on Starter and Business plans.")
    expense = db.execute("SELECT receipt_path FROM expenses WHERE id=? AND business_id=?", (expense_id, business_id)).fetchone()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found.")
    with transaction(db):
        db.execute("DELETE FROM expenses WHERE id=? AND business_id=?", (expense_id, business_id))
    if expense["receipt_path"]:
        try:
            path = Path(expense["receipt_path"]).resolve()
            if settings.storage_dir.resolve() in path.parents:
                path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}
