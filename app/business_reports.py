from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.business_base import _require
from app.db import get_db
from app.deps import get_business_ctx
from app.services import require_feature
from app.timeutils import date_range, utc_bounds

router = APIRouter(prefix="/api", tags=["reports"])


def _report_data(db: Any, business_id: str, kind: str, start_date, end_exclusive) -> tuple[list[dict], dict]:
    start, end = utc_bounds(start_date, end_exclusive)
    if kind == "sales":
        rows = db.execute(
            "SELECT s.sale_number,s.created_at,COALESCE(c.name,'Walk-in customer') AS customer_name,s.subtotal,s.discount,s.total,s.paid_amount,s.balance,s.payment_method,s.status "
            "FROM sales s LEFT JOIN customers c ON c.id=s.customer_id WHERE s.business_id=? AND s.created_at>=? AND s.created_at<? ORDER BY s.created_at DESC",
            (business_id, start, end),
        ).fetchall()
        summary = db.execute("SELECT COUNT(*) AS count,COALESCE(SUM(total),0) AS sales,COALESCE(SUM(paid_amount),0) AS paid,COALESCE(SUM(balance),0) AS outstanding FROM sales WHERE business_id=? AND created_at>=? AND created_at<?",
                             (business_id, start, end)).fetchone()
        return [dict(r) for r in rows], dict(summary)
    if kind == "expenses":
        rows = db.execute("SELECT expense_date,category,description,amount,payment_method FROM expenses WHERE business_id=? AND expense_date>=? AND expense_date<? ORDER BY expense_date DESC",
                           (business_id, start_date.isoformat(), end_exclusive.isoformat())).fetchall()
        total = sum(int(r["amount"]) for r in rows)
        return [dict(r) for r in rows], {"count": len(rows), "expenses": total}
    if kind == "profit":
        rows = db.execute(
            "SELECT s.sale_number,s.created_at,s.total,s.discount,COALESCE(SUM(si.line_total),0) AS item_revenue,"
            "COALESCE(SUM(si.unit_cost*si.quantity),0) AS cost_of_goods "
            "FROM sales s LEFT JOIN sale_items si ON si.sale_id=s.id AND si.business_id=s.business_id "
            "WHERE s.business_id=? AND s.created_at>=? AND s.created_at<? GROUP BY s.id ORDER BY s.created_at DESC",
            (business_id, start, end),
        ).fetchall()
        expenses = db.execute("SELECT COALESCE(SUM(amount),0) AS total FROM expenses WHERE business_id=? AND expense_date>=? AND expense_date<?",
                              (business_id, start_date.isoformat(), end_exclusive.isoformat())).fetchone()["total"]
        rows_out = []
        total_sales = total_cost = discounts = 0
        for row in rows:
            item = dict(row)
            total_sales += int(item["total"])
            total_cost += int(round(float(item["cost_of_goods"])))
            discounts += int(item["discount"])
            rows_out.append({**item, "gross_profit": int(item["item_revenue"]) - int(round(float(item["cost_of_goods"]))) - int(item["discount"])})
        gross_profit = total_sales - total_cost
        net_profit = gross_profit - int(expenses)
        return rows_out, {"sales": total_sales, "cost_of_goods": total_cost, "gross_profit": gross_profit,
                          "discounts": discounts, "expenses": int(expenses), "net_profit": net_profit,
                          "estimated": True}
    if kind == "inventory":
        rows = db.execute("SELECT name,sku,category,quantity,minimum_stock,purchase_price,selling_price,"
                          "(quantity*purchase_price) AS inventory_value FROM products WHERE business_id=? AND archived=0 ORDER BY name",
                          (business_id,)).fetchall()
        items = [dict(r) for r in rows]
        return items, {"products": len(items), "inventory_value": sum(float(r["inventory_value"]) for r in items),
                       "low_stock": sum(1 for r in items if 0 < float(r["quantity"]) <= float(r["minimum_stock"])),
                       "out_of_stock": sum(1 for r in items if float(r["quantity"]) <= 0)}
    if kind == "products":
        rows = db.execute("SELECT si.product_name,SUM(si.quantity) AS units,SUM(si.line_total) AS revenue,SUM(si.unit_cost*si.quantity) AS cost "
                          "FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE si.business_id=? AND s.created_at>=? AND s.created_at<? "
                          "GROUP BY si.product_name ORDER BY revenue DESC", (business_id, start, end)).fetchall()
        items = [dict(r) for r in rows]
        return items, {"products_sold": len(items), "units": sum(float(r["units"]) for r in items), "revenue": sum(int(r["revenue"]) for r in items)}
    if kind == "customers":
        rows = db.execute("SELECT c.name,c.phone,c.email,COUNT(s.id) AS orders,COALESCE(SUM(s.total),0) AS purchases,COALESCE(SUM(s.balance),0) AS outstanding "
                          "FROM customers c LEFT JOIN sales s ON s.customer_id=c.id AND s.business_id=c.business_id AND s.created_at>=? AND s.created_at<? "
                          "WHERE c.business_id=? AND c.archived=0 GROUP BY c.id ORDER BY purchases DESC LIMIT 500",
                          (start, end, business_id)).fetchall()
        items = [dict(r) for r in rows]
        return items, {"customers": len(items), "purchases": sum(int(r["purchases"]) for r in items)}
    raise HTTPException(status_code=422, detail="Choose a supported report type.")


def _check_report_feature(ctx: dict, kind: str) -> None:
    if kind == "sales":
        if "sales_reports" not in ctx["features"] and "basic_reports" not in ctx["features"]:
            require_feature(ctx, "sales_reports", "Sales reports are available on Starter and Business plans.")
    elif kind == "expenses":
        require_feature(ctx, "expense_analytics", "Expense analytics are available on the Business plan.")
    elif kind == "profit":
        require_feature(ctx, "profit_overview", "Profit overview is available on the Business plan.")
    elif kind == "products":
        require_feature(ctx, "sales_analytics", "Product performance analytics are available on the Business plan.")
    elif kind == "customers":
        require_feature(ctx, "customer_analytics", "Customer analytics are available on the Business plan.")
    elif kind != "inventory":
        raise HTTPException(status_code=422, detail="Choose a supported report type.")


def _make_report(db: Any, ctx: dict, kind: str, period: str, from_value: str | None, to_value: str | None) -> dict:
    _check_report_feature(ctx, kind)
    try:
        start_date, end_exclusive = date_range(period, from_value, to_value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    rows, summary = _report_data(db, ctx["business_id"], kind, start_date, end_exclusive)
    return {"type": kind, "period": period, "from": start_date.isoformat(), "to": (end_exclusive - timedelta(days=1)).isoformat(),
            "rows": rows, "summary": summary}


@router.get("/reports")
def reports(
    type: str = "sales",
    period: str = "month",
    from_date: str | None = None,
    to_date: str | None = None,
    ctx: dict = Depends(get_business_ctx),
    db=Depends(get_db),
):
    _require(ctx, "reports")
    if period not in {"today", "week", "month", "last_month", "custom"}:
        raise HTTPException(status_code=422, detail="Choose a supported reporting period.")
    return _make_report(db, ctx, type, period, from_date, to_date)


def _safe_csv_cell(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + value
    return value


@router.get("/reports/export.csv")
def export_report(
    type: str = "sales",
    period: str = "month",
    from_date: str | None = None,
    to_date: str | None = None,
    ctx: dict = Depends(get_business_ctx),
    db=Depends(get_db),
):
    _require(ctx, "reports")
    require_feature(ctx, "data_export", "CSV data export is available on the Business plan.")
    if period not in {"today", "week", "month", "last_month", "custom"}:
        raise HTTPException(status_code=422, detail="Choose a supported reporting period.")
    report = _make_report(db, ctx, type, period, from_date, to_date)
    output = io.StringIO()
    if report["rows"]:
        columns = list(report["rows"][0].keys())
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in report["rows"]:
            writer.writerow({k: _safe_csv_cell(row.get(k, "")) for k in columns})
    else:
        writer = csv.writer(output)
        writer.writerow(["No records for this period"])
    filename = f"bizflow-{type}-{report['from']}-to-{report['to']}.csv"
    return Response(content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
