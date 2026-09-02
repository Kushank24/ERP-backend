from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _fmt_month(dt: datetime) -> str:
    return dt.strftime("%b %y")


# ─────────────────────────────────────────────────────────────────────────────
# CRM ANALYTICS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/crm")
def crm_analytics(
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
):
    # Build separate filters for enquiries (enquiry_date) and offers (offer_date)
    enq_filter = ""
    enq_params: dict = {}
    if date_from:
        enq_filter += " AND enquiry_date >= :enq_date_from"
        enq_params["enq_date_from"] = date_from
    if date_to:
        enq_filter += " AND enquiry_date <= :enq_date_to"
        enq_params["enq_date_to"] = date_to

    off_filter = ""
    off_params: dict = {}
    if date_from:
        off_filter += " AND offer_date >= :off_date_from"
        off_params["off_date_from"] = date_from
    if date_to:
        off_filter += " AND offer_date <= :off_date_to"
        off_params["off_date_to"] = date_to

    totals_row = db.execute(text(f"""
        SELECT
          (SELECT COUNT(*) FROM enquiries WHERE 1=1{enq_filter})                                 AS enquiries,
          (SELECT COUNT(*) FROM offers WHERE 1=1{off_filter})                                    AS offers,
          (SELECT COUNT(*) FROM offers WHERE status = 'accepted'{off_filter})                    AS accepted,
          (SELECT COUNT(*) FROM offers WHERE status = 'rejected'{off_filter})                    AS rejected,
          (SELECT COUNT(*) FROM offers WHERE status = 'sent'{off_filter})                        AS open,
          (SELECT COUNT(*) FROM offers WHERE status = 'draft'{off_filter})                       AS draft,
          (SELECT COUNT(*) FROM companies)                                                        AS companies
    """), {**enq_params, **off_params}).mappings().first()

    pipeline_row = db.execute(text(f"""
        SELECT
          COALESCE(SUM(CASE WHEN status = 'sent'     THEN total_amount ELSE 0 END), 0) AS open_value,
          COALESCE(SUM(CASE WHEN status = 'accepted' THEN total_amount ELSE 0 END), 0) AS won_value,
          CASE WHEN COUNT(*) > 0 THEN COALESCE(AVG(total_amount), 0) ELSE 0 END        AS avg_offer_value,
          CASE
            WHEN COUNT(*) FILTER (WHERE status IN ('accepted','rejected')) > 0
            THEN ROUND(
              (100.0 * COUNT(*) FILTER (WHERE status = 'accepted')
                   / COUNT(*) FILTER (WHERE status IN ('accepted','rejected')))::numeric, 1)
            ELSE 0
          END AS win_rate_pct
        FROM offers
        WHERE 1=1{off_filter}
    """), off_params).mappings().first()

    # Monthly activity — use date range if provided, else last 13 months
    if date_from or date_to:
        off_monthly_where = f"WHERE 1=1{off_filter}"
        enq_monthly_where = f"WHERE 1=1{enq_filter}"
    else:
        off_monthly_where = "WHERE offer_date >= DATE_TRUNC('month', NOW()) - INTERVAL '12 months'"
        enq_monthly_where = "WHERE enquiry_date >= DATE_TRUNC('month', NOW()) - INTERVAL '12 months'"

    monthly_rows = db.execute(text(f"""
        WITH months AS (
          SELECT DATE_TRUNC('month', offer_date)                                                 AS mo,
                 COUNT(*)                                                                        AS offers,
                 COUNT(*) FILTER (WHERE status = 'accepted')                                    AS won,
                 COALESCE(SUM(CASE WHEN status = 'accepted' THEN total_amount ELSE 0 END), 0)   AS won_value
          FROM offers
          {off_monthly_where}
          GROUP BY mo
        ),
        enq_months AS (
          SELECT DATE_TRUNC('month', enquiry_date) AS mo, COUNT(*) AS enquiries
          FROM enquiries
          {enq_monthly_where}
          GROUP BY mo
        )
        SELECT
          COALESCE(m.mo, e.mo) AS mo,
          COALESCE(e.enquiries, 0) AS enquiries,
          COALESCE(m.offers,    0) AS offers,
          COALESCE(m.won,       0) AS won,
          COALESCE(m.won_value, 0) AS won_value
        FROM months m
        FULL OUTER JOIN enq_months e ON e.mo = m.mo
        ORDER BY mo ASC
    """), {**enq_params, **off_params}).mappings().all()

    monthly = []
    for r in monthly_rows:
        mo = r["mo"]
        if mo is None:
            continue
        if isinstance(mo, str):
            mo = datetime.fromisoformat(mo)
        monthly.append({
            "month":     _fmt_month(mo),
            "enquiries": int(r["enquiries"] or 0),
            "offers":    int(r["offers"]    or 0),
            "won":       int(r["won"]       or 0),
            "won_value": float(r["won_value"] or 0),
        })

    return {
        "totals": {
            "enquiries": int(totals_row["enquiries"] or 0),
            "offers":    int(totals_row["offers"]    or 0),
            "accepted":  int(totals_row["accepted"]  or 0),
            "rejected":  int(totals_row["rejected"]  or 0),
            "open":      int(totals_row["open"]      or 0),
            "draft":     int(totals_row["draft"]     or 0),
            "companies": int(totals_row["companies"] or 0),
        },
        "pipeline": {
            "open_value":      float(pipeline_row["open_value"]      or 0),
            "won_value":       float(pipeline_row["won_value"]       or 0),
            "avg_offer_value": float(pipeline_row["avg_offer_value"] or 0),
            "win_rate_pct":    float(pipeline_row["win_rate_pct"]    or 0),
        },
        "monthly": monthly,
    }


# ─────────────────────────────────────────────────────────────────────────────
# COMPANY LIST (for dropdown)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/companies")
def list_companies_summary(
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
):
    # Apply date filters to JOIN conditions so counts reflect the period
    enq_join_filter = ""
    off_join_filter = ""
    params: dict = {}
    if date_from:
        enq_join_filter += " AND e.enquiry_date >= :date_from"
        off_join_filter += " AND o.offer_date >= :date_from"
        params["date_from"] = date_from
    if date_to:
        enq_join_filter += " AND e.enquiry_date <= :date_to"
        off_join_filter += " AND o.offer_date <= :date_to"
        params["date_to"] = date_to

    rows = db.execute(text(f"""
        SELECT c.id, c.name,
               COUNT(DISTINCT e.id)  AS enquiry_count,
               COUNT(DISTINCT o.id)  AS offer_count,
               COALESCE(MAX(e.enquiry_date), MAX(o.offer_date)) AS last_activity
        FROM companies c
        LEFT JOIN enquiries e ON e.company_id = c.id{enq_join_filter}
        LEFT JOIN offers    o ON o.company_id = c.id{off_join_filter}
        GROUP BY c.id, c.name
        ORDER BY last_activity DESC NULLS LAST, c.name
    """), params).mappings().all()

    return [
        {
            "id":             r["id"],
            "name":           r["name"],
            "enquiry_count":  int(r["enquiry_count"] or 0),
            "offer_count":    int(r["offer_count"]   or 0),
            "last_activity":  str(r["last_activity"]) if r["last_activity"] else None,
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# COMPANY DEEP-DIVE
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/company/{company_id}")
def company_analytics(
    company_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
):
    company = db.execute(
        text("SELECT id, name, contact_person, phone, email, gstin FROM companies WHERE id = :id"),
        {"id": company_id},
    ).mappings().first()
    if not company:
        raise HTTPException(404, "Company not found")

    # Build date filters
    enq_filter = ""
    off_filter = ""
    date_params: dict = {}
    if date_from:
        enq_filter += " AND enquiry_date >= :date_from"
        off_filter += " AND offer_date >= :date_from"
        date_params["date_from"] = date_from
    if date_to:
        enq_filter += " AND enquiry_date <= :date_to"
        off_filter += " AND offer_date <= :date_to"
        date_params["date_to"] = date_to

    # Enquiry timeline
    enquiry_rows = db.execute(text(f"""
        SELECT id, enquiry_number, enquiry_date, status, priority, reference_number, notes
        FROM enquiries WHERE company_id = :id{enq_filter}
        ORDER BY enquiry_date DESC
    """), {"id": company_id, **date_params}).mappings().all()

    # Offer timeline with items
    offer_rows = db.execute(text(f"""
        SELECT o.id, o.offer_number, o.offer_date, o.status, o.total_amount,
               o.follow_up_comments, o.enquiry_id
        FROM offers o WHERE o.company_id = :id{off_filter}
        ORDER BY o.offer_date DESC
    """), {"id": company_id, **date_params}).mappings().all()

    offer_ids = [r["id"] for r in offer_rows]

    # Products per offer
    products_by_offer: dict[int, list] = {oid: [] for oid in offer_ids}
    if offer_ids:
        product_rows = db.execute(text("""
            SELECT oi.offer_id, COALESCE(cp.model_name, oi.description) AS product,
                   oi.quantity, oi.unit_price, oi.total_price
            FROM offer_items oi
            LEFT JOIN catalog_products cp ON cp.id = oi.product_id
            WHERE oi.offer_id = ANY(:ids)
            ORDER BY oi.offer_id, oi.id
        """), {"ids": offer_ids}).mappings().all()
        for row in product_rows:
            products_by_offer[row["offer_id"]].append({
                "product":    row["product"],
                "quantity":   int(row["quantity"] or 1),
                "unit_price": float(row["unit_price"] or 0),
                "total":      float(row["total_price"] or 0),
            })

    offers_list = [
        {
            "id":               r["id"],
            "offer_number":     r["offer_number"],
            "offer_date":       str(r["offer_date"]) if r["offer_date"] else None,
            "status":           r["status"],
            "total_amount":     float(r["total_amount"] or 0),
            "follow_up":        r["follow_up_comments"],
            "enquiry_id":       r["enquiry_id"],
            "products":         products_by_offer[r["id"]],
        }
        for r in offer_rows
    ]

    enquiries_list = [
        {
            "id":              r["id"],
            "enquiry_number":  r["enquiry_number"],
            "enquiry_date":    str(r["enquiry_date"]) if r["enquiry_date"] else None,
            "status":          r["status"],
            "priority":        r["priority"],
            "reference_number": r["reference_number"],
        }
        for r in enquiry_rows
    ]

    # Product frequency across all offers
    product_freq: dict[str, int] = {}
    for items in products_by_offer.values():
        for item in items:
            name = item["product"] or "Unknown"
            product_freq[name] = product_freq.get(name, 0) + item["quantity"]

    top_products = sorted(
        [{"product": k, "quantity": v} for k, v in product_freq.items()],
        key=lambda x: x["quantity"], reverse=True
    )[:10]

    # Monthly trend — use supplied date range or fall back to last 24 months
    if date_from or date_to:
        enq_trend_filter = enq_filter
        off_trend_filter = off_filter
    else:
        enq_trend_filter = " AND enquiry_date >= NOW() - INTERVAL '24 months'"
        off_trend_filter = " AND offer_date >= NOW() - INTERVAL '24 months'"

    trend_rows = db.execute(text(f"""
        WITH e_mo AS (
          SELECT DATE_TRUNC('month', enquiry_date) AS mo, COUNT(*) AS cnt
          FROM enquiries WHERE company_id = :id{enq_trend_filter}
          GROUP BY mo
        ),
        o_mo AS (
          SELECT DATE_TRUNC('month', offer_date) AS mo,
                 COUNT(*) AS cnt,
                 COUNT(*) FILTER (WHERE status = 'accepted') AS won,
                 COALESCE(SUM(CASE WHEN status='accepted' THEN total_amount ELSE 0 END),0) AS won_val
          FROM offers WHERE company_id = :id{off_trend_filter}
          GROUP BY mo
        )
        SELECT COALESCE(e.mo, o.mo) AS mo,
               COALESCE(e.cnt, 0) AS enquiries,
               COALESCE(o.cnt, 0) AS offers,
               COALESCE(o.won, 0) AS won,
               COALESCE(o.won_val, 0) AS won_value
        FROM e_mo e
        FULL OUTER JOIN o_mo o ON o.mo = e.mo
        ORDER BY mo ASC
    """), {"id": company_id, **date_params}).mappings().all()

    monthly_trend = []
    for r in trend_rows:
        mo = r["mo"]
        if mo is None:
            continue
        if isinstance(mo, str):
            mo = datetime.fromisoformat(mo)
        monthly_trend.append({
            "month":     _fmt_month(mo),
            "enquiries": int(r["enquiries"] or 0),
            "offers":    int(r["offers"] or 0),
            "won":       int(r["won"] or 0),
            "won_value": float(r["won_value"] or 0),
        })

    # Summary stats
    total_enq   = len(enquiries_list)
    total_off   = len(offers_list)
    accepted    = sum(1 for o in offers_list if o["status"] == "accepted")
    total_won   = sum(o["total_amount"] for o in offers_list if o["status"] == "accepted")
    win_rate    = round(100.0 * accepted / total_off, 1) if total_off else 0.0

    return {
        "company": {
            "id":             company["id"],
            "name":           company["name"],
            "contact_person": company["contact_person"],
            "phone":          company["phone"],
            "email":          company["email"],
            "gstin":          company["gstin"],
        },
        "summary": {
            "total_enquiries": total_enq,
            "total_offers":    total_off,
            "accepted_offers": accepted,
            "win_rate_pct":    win_rate,
            "total_won_value": total_won,
        },
        "enquiries":     enquiries_list,
        "offers":        offers_list,
        "top_products":  top_products,
        "monthly_trend": monthly_trend,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTION ANALYTICS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/production")
def production_analytics(
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
):
    date_filter = ""
    date_params: dict = {}
    if date_from:
        date_filter += " AND creation_date >= :date_from"
        date_params["date_from"] = date_from
    if date_to:
        date_filter += " AND creation_date <= :date_to"
        date_params["date_to"] = date_to

    # Work order counts by status
    wo_rows = db.execute(text(f"""
        SELECT status, COUNT(*) AS cnt FROM work_orders
        WHERE 1=1{date_filter}
        GROUP BY status
    """), date_params).mappings().all()

    wo_by_status: dict[str, int] = {r["status"]: int(r["cnt"] or 0) for r in wo_rows}
    total_wo    = sum(wo_by_status.values())
    completed   = wo_by_status.get("completed",   0)
    in_progress = wo_by_status.get("in-progress", 0)
    pending     = wo_by_status.get("pending",     0)
    cancelled   = wo_by_status.get("cancelled",   0)
    completion_rate = round(100.0 * completed / total_wo, 1) if total_wo else 0.0

    # WO delivery performance (overdue = delivery_date < today AND not completed)
    perf_row = db.execute(text(f"""
        SELECT
          COUNT(*) FILTER (WHERE delivery_date < CURRENT_DATE AND status != 'completed') AS overdue,
          COUNT(*) FILTER (WHERE delivery_date IS NOT NULL)                               AS with_deadline
        FROM work_orders
        WHERE 1=1{date_filter}
    """), date_params).mappings().first()

    # Inventory health — current state, no date filter
    inv_row = db.execute(text("""
        SELECT
          COUNT(*)                                                            AS total_materials,
          COALESCE(SUM(length_weight_nos * per_unit_cost), 0)                AS total_value,
          COUNT(*) FILTER (WHERE length_weight_nos > 0 AND length_weight_nos < 10)  AS low_stock,
          COUNT(*) FILTER (WHERE length_weight_nos <= 0)                     AS out_of_stock,
          COUNT(*) FILTER (WHERE length_weight_nos >= 10)                    AS healthy
        FROM materials
    """)).mappings().first()

    # Monthly work orders — use date range if provided, else last 13 months
    if date_from or date_to:
        monthly_wo_where = f"WHERE 1=1{date_filter}"
    else:
        monthly_wo_where = "WHERE creation_date >= DATE_TRUNC('month', NOW()) - INTERVAL '12 months'"

    monthly_wo_rows = db.execute(text(f"""
        SELECT DATE_TRUNC('month', creation_date)                             AS mo,
               COUNT(*)                                                       AS cnt,
               COUNT(*) FILTER (WHERE status = 'completed')                  AS done
        FROM work_orders
        {monthly_wo_where}
        GROUP BY mo
        ORDER BY mo ASC
    """), date_params).mappings().all()

    monthly_wo = []
    for r in monthly_wo_rows:
        mo = r["mo"]
        if mo is None:
            continue
        if isinstance(mo, str):
            mo = datetime.fromisoformat(mo)
        monthly_wo.append({
            "month": _fmt_month(mo),
            "total": int(r["cnt"]  or 0),
            "done":  int(r["done"] or 0),
        })

    # Top external clients
    top_clients_rows = db.execute(text(f"""
        SELECT party_name AS name, COUNT(*) AS count,
               COUNT(*) FILTER (WHERE status = 'completed') AS completed
        FROM work_orders
        WHERE party_name IS NOT NULL AND party_name <> ''
          AND LOWER(party_name) NOT LIKE '%e-safe%'
          AND LOWER(party_name) NOT LIKE '%e safe%'
          AND LOWER(party_name) NOT LIKE '%esafe%'
          {date_filter}
        GROUP BY party_name
        ORDER BY count DESC
        LIMIT 10
    """), date_params).mappings().all()

    top_clients = [
        {
            "name":      r["name"],
            "count":     int(r["count"]     or 0),
            "completed": int(r["completed"] or 0),
        }
        for r in top_clients_rows
    ]

    # Top materials by value — current inventory state, no date filter
    top_materials_rows = db.execute(text("""
        SELECT name,
               COALESCE(length_weight_nos * per_unit_cost, 0) AS value,
               COALESCE(length_weight_nos, 0)                 AS qty,
               unit
        FROM materials
        ORDER BY value DESC
        LIMIT 10
    """)).mappings().all()

    top_materials = [
        {
            "name":  r["name"],
            "value": float(r["value"] or 0),
            "qty":   float(r["qty"]   or 0),
            "unit":  r["unit"] or "",
        }
        for r in top_materials_rows
    ]

    # Monthly material additions (last 13 months) — uses created_at, keep as-is
    mat_monthly_rows = db.execute(text("""
        SELECT DATE_TRUNC('month', created_at) AS mo, COUNT(*) AS cnt
        FROM materials
        WHERE created_at >= DATE_TRUNC('month', NOW()) - INTERVAL '12 months'
        GROUP BY mo ORDER BY mo ASC
    """)).mappings().all()

    mat_monthly = []
    for r in mat_monthly_rows:
        mo = r["mo"]
        if mo is None:
            continue
        if isinstance(mo, str):
            mo = datetime.fromisoformat(mo)
        mat_monthly.append({"month": _fmt_month(mo), "count": int(r["cnt"] or 0)})

    return {
        "work_orders": {
            "total":               total_wo,
            "in_progress":         in_progress,
            "completed":           completed,
            "pending":             pending,
            "cancelled":           cancelled,
            "completion_rate_pct": completion_rate,
            "overdue":             int(perf_row["overdue"]       or 0),
            "with_deadline":       int(perf_row["with_deadline"] or 0),
        },
        "inventory": {
            "total_materials": int(inv_row["total_materials"] or 0),
            "total_value":     float(inv_row["total_value"]   or 0),
            "low_stock":       int(inv_row["low_stock"]       or 0),
            "out_of_stock":    int(inv_row["out_of_stock"]    or 0),
            "healthy":         int(inv_row["healthy"]         or 0),
        },
        "monthly_wo":    monthly_wo,
        "top_clients":   top_clients,
        "top_materials": top_materials,
        "mat_monthly":   mat_monthly,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTION CLIENT LIST
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/production/clients")
def list_production_clients(
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
):
    date_filter = ""
    date_params: dict = {}
    if date_from:
        date_filter += " AND creation_date >= :date_from"
        date_params["date_from"] = date_from
    if date_to:
        date_filter += " AND creation_date <= :date_to"
        date_params["date_to"] = date_to

    rows = db.execute(text(f"""
        SELECT party_name AS name,
               COUNT(*)                                               AS total,
               COUNT(*) FILTER (WHERE status = 'completed')          AS completed,
               COUNT(*) FILTER (WHERE status = 'in-progress')        AS in_progress,
               MAX(creation_date)                                     AS last_wo
        FROM work_orders
        WHERE party_name IS NOT NULL AND party_name <> ''
          AND LOWER(party_name) NOT LIKE '%e-safe%'
          AND LOWER(party_name) NOT LIKE '%e safe%'
          AND LOWER(party_name) NOT LIKE '%esafe%'
          {date_filter}
        GROUP BY party_name
        ORDER BY last_wo DESC NULLS LAST, total DESC
    """), date_params).mappings().all()

    return [
        {
            "name":        r["name"],
            "total":       int(r["total"]       or 0),
            "completed":   int(r["completed"]   or 0),
            "in_progress": int(r["in_progress"] or 0),
            "last_wo":     str(r["last_wo"]) if r["last_wo"] else None,
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTION CLIENT DEEP-DIVE
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/production/client")
def production_client_analytics(
    name: str = Query(..., description="party_name to analyse"),
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
):
    date_filter = ""
    date_params: dict = {}
    if date_from:
        date_filter += " AND creation_date >= :date_from"
        date_params["date_from"] = date_from
    if date_to:
        date_filter += " AND creation_date <= :date_to"
        date_params["date_to"] = date_to

    # Work orders for this client
    wo_rows = db.execute(text(f"""
        SELECT id, work_order_number, po_number, creation_date, delivery_date, status, remarks
        FROM work_orders
        WHERE party_name = :name{date_filter}
        ORDER BY creation_date DESC
    """), {"name": name, **date_params}).mappings().all()

    if not wo_rows:
        raise HTTPException(404, "No work orders found for this client")

    wo_ids = [r["id"] for r in wo_rows]

    # Products per work order
    products_by_wo: dict[int, list] = {wid: [] for wid in wo_ids}
    prod_rows = db.execute(text("""
        SELECT wp.work_order_id, p.name AS product, wp.quantity
        FROM work_order_products wp
        JOIN products p ON p.id = wp.product_id
        WHERE wp.work_order_id = ANY(:ids)
        ORDER BY wp.work_order_id, p.name
    """), {"ids": wo_ids}).mappings().all()
    for row in prod_rows:
        products_by_wo[row["work_order_id"]].append({
            "product":  row["product"],
            "quantity": int(row["quantity"] or 1),
        })

    wo_list = [
        {
            "id":               r["id"],
            "wo_number":        r["work_order_number"],
            "po_number":        r["po_number"],
            "creation_date":    str(r["creation_date"])  if r["creation_date"]  else None,
            "delivery_date":    str(r["delivery_date"])  if r["delivery_date"]  else None,
            "status":           r["status"],
            "remarks":          r["remarks"],
            "products":         products_by_wo[r["id"]],
        }
        for r in wo_rows
    ]

    # Summary stats
    total     = len(wo_list)
    completed = sum(1 for w in wo_list if w["status"] == "completed")
    overdue   = sum(
        1 for w in wo_list
        if w["delivery_date"] and w["status"] != "completed"
        and w["delivery_date"] < str(datetime.now().date())
    )
    comp_rate = round(100.0 * completed / total, 1) if total else 0.0

    # Product frequency
    prod_freq: dict[str, int] = {}
    for items in products_by_wo.values():
        for item in items:
            prod_freq[item["product"]] = prod_freq.get(item["product"], 0) + item["quantity"]
    top_products = sorted(
        [{"product": k, "quantity": v} for k, v in prod_freq.items()],
        key=lambda x: x["quantity"], reverse=True
    )[:10]

    # Monthly trend — use supplied date range or fall back to last 24 months
    if date_from or date_to:
        trend_date_filter = date_filter
    else:
        trend_date_filter = " AND creation_date >= NOW() - INTERVAL '24 months'"

    trend_rows = db.execute(text(f"""
        SELECT DATE_TRUNC('month', creation_date)                      AS mo,
               COUNT(*)                                                AS total,
               COUNT(*) FILTER (WHERE status = 'completed')           AS completed
        FROM work_orders
        WHERE party_name = :name{trend_date_filter}
        GROUP BY mo ORDER BY mo ASC
    """), {"name": name, **date_params}).mappings().all()

    monthly_trend = []
    for r in trend_rows:
        mo = r["mo"]
        if mo is None:
            continue
        if isinstance(mo, str):
            mo = datetime.fromisoformat(mo)
        monthly_trend.append({
            "month":     _fmt_month(mo),
            "total":     int(r["total"]     or 0),
            "completed": int(r["completed"] or 0),
        })

    return {
        "client": {"name": name},
        "summary": {
            "total_wo":        total,
            "completed":       completed,
            "overdue":         overdue,
            "completion_rate": comp_rate,
        },
        "work_orders":   wo_list,
        "top_products":  top_products,
        "monthly_trend": monthly_trend,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PURCHASE ORDER ANALYTICS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/po")
def po_analytics(
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
):
    date_filter = ""
    date_params: dict = {}
    if date_from:
        date_filter += " AND purchase_date >= :date_from"
        date_params["date_from"] = date_from
    if date_to:
        date_filter += " AND purchase_date <= :date_to"
        date_params["date_to"] = date_to

    totals_row = db.execute(text(f"""
        SELECT
          COUNT(*)                                                                   AS total,
          COALESCE(SUM(total_amount), 0)                                             AS total_value,
          COUNT(*) FILTER (WHERE status = 1)                                         AS pending,
          COUNT(*) FILTER (WHERE status = 2)                                         AS confirmed,
          COUNT(*) FILTER (WHERE status = 3)                                         AS partial,
          COUNT(*) FILTER (WHERE status = 4)                                         AS delivered,
          COUNT(*) FILTER (WHERE status = 5)                                         AS cancelled,
          COUNT(*) FILTER (
            WHERE order_delivery_date < CURRENT_DATE AND status NOT IN (4, 5)
          )                                                                           AS overdue
        FROM purchase_orders
        WHERE 1=1{date_filter}
    """), date_params).mappings().first()

    # Monthly POs — use date range if provided, else last 13 months
    if date_from or date_to:
        monthly_where = f"WHERE 1=1{date_filter}"
    else:
        monthly_where = "WHERE purchase_date >= DATE_TRUNC('month', NOW()) - INTERVAL '12 months'"

    monthly_rows = db.execute(text(f"""
        SELECT DATE_TRUNC('month', purchase_date) AS mo,
               COUNT(*)                           AS cnt,
               COALESCE(SUM(total_amount), 0)     AS value
        FROM purchase_orders
        {monthly_where}
        GROUP BY mo
        ORDER BY mo ASC
    """), date_params).mappings().all()

    monthly = []
    for r in monthly_rows:
        mo = r["mo"]
        if mo is None:
            continue
        if isinstance(mo, str):
            mo = datetime.fromisoformat(mo)
        monthly.append({
            "month": _fmt_month(mo),
            "count": int(r["cnt"]   or 0),
            "value": float(r["value"] or 0),
        })

    # Top 10 suppliers by total PO value
    top_suppliers_rows = db.execute(text(f"""
        SELECT s.name, COALESCE(SUM(p.total_amount), 0) AS total_amount
        FROM purchase_orders p
        JOIN suppliers s ON s.id = p.supplier_id
        WHERE 1=1{date_filter}
        GROUP BY s.name
        ORDER BY total_amount DESC
        LIMIT 10
    """), date_params).mappings().all()

    top_suppliers = [
        {"name": r["name"], "total_amount": float(r["total_amount"] or 0)}
        for r in top_suppliers_rows
    ]

    # Status breakdown
    status_rows = db.execute(text(f"""
        SELECT status, COUNT(*) AS cnt FROM purchase_orders
        WHERE 1=1{date_filter}
        GROUP BY status ORDER BY status
    """), date_params).mappings().all()

    status_breakdown = [
        {"status": int(r["status"] or 0), "count": int(r["cnt"] or 0)}
        for r in status_rows
    ]

    return {
        "totals": {
            "total":       int(totals_row["total"]     or 0),
            "total_value": float(totals_row["total_value"] or 0),
            "pending":     int(totals_row["pending"]   or 0),
            "confirmed":   int(totals_row["confirmed"] or 0),
            "partial":     int(totals_row["partial"]   or 0),
            "delivered":   int(totals_row["delivered"] or 0),
            "cancelled":   int(totals_row["cancelled"] or 0),
            "overdue":     int(totals_row["overdue"]   or 0),
        },
        "monthly":          monthly,
        "top_suppliers":    top_suppliers,
        "status_breakdown": status_breakdown,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PO SUPPLIER LIST
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/po/suppliers")
def list_po_suppliers(db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    rows = db.execute(text("""
        SELECT s.name,
               COUNT(p.id)          AS total_pos,
               MAX(p.purchase_date) AS last_po
        FROM purchase_orders p
        JOIN suppliers s ON s.id = p.supplier_id
        GROUP BY s.name
        ORDER BY last_po DESC NULLS LAST, total_pos DESC
    """)).mappings().all()

    return [
        {
            "name":       r["name"],
            "total_pos":  int(r["total_pos"] or 0),
            "last_po":    str(r["last_po"]) if r["last_po"] else None,
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# PO SUPPLIER DEEP-DIVE
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/po/supplier")
def po_supplier_analytics(
    name: str = Query(..., description="Supplier name"),
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    # Verify supplier exists
    supplier = db.execute(
        text("SELECT id FROM suppliers WHERE name = :name"),
        {"name": name},
    ).first()
    if not supplier:
        raise HTTPException(404, "Supplier not found")

    supplier_id = supplier[0]

    # PO list with summary data
    po_rows = db.execute(text("""
        SELECT id, purchase_number, purchase_date, order_delivery_date,
               actual_delivery_date, status, total_amount
        FROM purchase_orders
        WHERE supplier_id = :sid
        ORDER BY purchase_date DESC
    """), {"sid": supplier_id}).mappings().all()

    po_ids = [r["id"] for r in po_rows]

    # Lines per PO
    lines_by_po: dict[int, list] = {pid: [] for pid in po_ids}
    if po_ids:
        line_rows = db.execute(text("""
            SELECT purchase_order_id, material_name, length_weight_nos, unit, per_unit_cost
            FROM purchase_order_lines
            WHERE purchase_order_id = ANY(:ids)
            ORDER BY purchase_order_id, id
        """), {"ids": po_ids}).mappings().all()
        for row in line_rows:
            lines_by_po[row["purchase_order_id"]].append({
                "material_name":    row["material_name"],
                "length_weight_nos": float(row["length_weight_nos"] or 0),
                "unit":             row["unit"] or "",
                "per_unit_cost":    float(row["per_unit_cost"] or 0),
            })

    today_str = str(datetime.now().date())
    pos_list = []
    for r in po_rows:
        pos_list.append({
            "id":                   r["id"],
            "purchase_number":      r["purchase_number"],
            "purchase_date":        str(r["purchase_date"])         if r["purchase_date"]         else None,
            "order_delivery_date":  str(r["order_delivery_date"])   if r["order_delivery_date"]   else None,
            "actual_delivery_date": str(r["actual_delivery_date"])  if r["actual_delivery_date"]  else None,
            "status":               int(r["status"] or 0),
            "total_amount":         float(r["total_amount"] or 0),
            "lines":                lines_by_po[r["id"]],
        })

    # Summary stats
    total_pos   = len(pos_list)
    total_value = sum(p["total_amount"] for p in pos_list)
    delivered   = sum(1 for p in pos_list if p["status"] == 4)
    overdue     = sum(
        1 for p in pos_list
        if p["order_delivery_date"]
        and p["status"] not in (4, 5)
        and p["order_delivery_date"] < today_str
    )

    # Avg delivery days (where actual_delivery_date is set)
    avg_row = db.execute(text("""
        SELECT AVG((actual_delivery_date - purchase_date)::integer) AS avg_days
        FROM purchase_orders
        WHERE supplier_id = :sid AND actual_delivery_date IS NOT NULL
    """), {"sid": supplier_id}).first()
    avg_delivery_days = round(float(avg_row[0]), 1) if avg_row and avg_row[0] is not None else None

    # Top 10 materials
    mat_freq: dict[str, float] = {}
    for p in pos_list:
        for line in p["lines"]:
            name_key = line["material_name"] or "Unknown"
            mat_freq[name_key] = mat_freq.get(name_key, 0.0) + line["length_weight_nos"]
    top_materials = sorted(
        [{"material_name": k, "total_qty": round(v, 2)} for k, v in mat_freq.items()],
        key=lambda x: x["total_qty"], reverse=True
    )[:10]

    # Monthly trend last 24 months
    trend_rows = db.execute(text("""
        SELECT DATE_TRUNC('month', purchase_date) AS mo,
               COUNT(*)                           AS cnt,
               COALESCE(SUM(total_amount), 0)     AS value
        FROM purchase_orders
        WHERE supplier_id = :sid
          AND purchase_date >= NOW() - INTERVAL '24 months'
        GROUP BY mo ORDER BY mo ASC
    """), {"sid": supplier_id}).mappings().all()

    monthly_trend = []
    for r in trend_rows:
        mo = r["mo"]
        if mo is None:
            continue
        if isinstance(mo, str):
            mo = datetime.fromisoformat(mo)
        monthly_trend.append({
            "month": _fmt_month(mo),
            "count": int(r["cnt"]   or 0),
            "value": float(r["value"] or 0),
        })

    return {
        "summary": {
            "total_pos":          total_pos,
            "total_value":        total_value,
            "delivered":          delivered,
            "overdue":            overdue,
            "avg_delivery_days":  avg_delivery_days,
        },
        "pos":           pos_list,
        "top_materials": top_materials,
        "monthly_trend": monthly_trend,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SALES ORDER ANALYTICS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/so")
def so_analytics(
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
):
    date_filter = ""
    date_params: dict = {}
    if date_from:
        date_filter += " AND sales_date >= :date_from"
        date_params["date_from"] = date_from
    if date_to:
        date_filter += " AND sales_date <= :date_to"
        date_params["date_to"] = date_to

    totals_row = db.execute(text(f"""
        SELECT
          COUNT(*)                                                                       AS total,
          COALESCE(SUM(total_amount), 0)                                                 AS total_revenue,
          COUNT(*) FILTER (WHERE status = 1)                                             AS not_received,
          COUNT(*) FILTER (WHERE status = 2)                                             AS partial,
          COUNT(*) FILTER (WHERE status = 3)                                             AS received,
          COUNT(*) FILTER (
            WHERE delivery_date < CURRENT_DATE AND actual_delivery_date IS NULL
          )                                                                               AS overdue,
          CASE
            WHEN COALESCE(SUM(total_amount), 0) > 0
            THEN ROUND(
              (100.0 * COALESCE(SUM(payment_amount) FILTER (WHERE payment_amount IS NOT NULL), 0)
                    / COALESCE(SUM(total_amount), 1))::numeric, 1)
            ELSE 0
          END                                                                             AS payment_collection_rate
        FROM sales_orders
        WHERE 1=1{date_filter}
    """), date_params).mappings().first()

    # Monthly SOs — use date range if provided, else last 13 months
    if date_from or date_to:
        monthly_where = f"WHERE 1=1{date_filter}"
    else:
        monthly_where = "WHERE sales_date >= DATE_TRUNC('month', NOW()) - INTERVAL '12 months'"

    monthly_rows = db.execute(text(f"""
        SELECT DATE_TRUNC('month', sales_date)                                     AS mo,
               COUNT(*)                                                            AS cnt,
               COALESCE(SUM(total_amount), 0)                                      AS value,
               COALESCE(SUM(payment_amount) FILTER (WHERE payment_amount IS NOT NULL), 0) AS payment
        FROM sales_orders
        {monthly_where}
        GROUP BY mo
        ORDER BY mo ASC
    """), date_params).mappings().all()

    monthly = []
    for r in monthly_rows:
        mo = r["mo"]
        if mo is None:
            continue
        if isinstance(mo, str):
            mo = datetime.fromisoformat(mo)
        monthly.append({
            "month":   _fmt_month(mo),
            "count":   int(r["cnt"]     or 0),
            "value":   float(r["value"]   or 0),
            "payment": float(r["payment"] or 0),
        })

    # Top 10 customers by total_amount
    top_customers_rows = db.execute(text(f"""
        SELECT company_name, COALESCE(SUM(total_amount), 0) AS total_amount
        FROM sales_orders
        WHERE 1=1{date_filter}
        GROUP BY company_name
        ORDER BY total_amount DESC
        LIMIT 10
    """), date_params).mappings().all()

    top_customers = [
        {"company_name": r["company_name"], "total_amount": float(r["total_amount"] or 0)}
        for r in top_customers_rows
    ]

    return {
        "totals": {
            "total":                    int(totals_row["total"]                    or 0),
            "total_revenue":            float(totals_row["total_revenue"]          or 0),
            "not_received":             int(totals_row["not_received"]             or 0),
            "partial":                  int(totals_row["partial"]                  or 0),
            "received":                 int(totals_row["received"]                 or 0),
            "overdue":                  int(totals_row["overdue"]                  or 0),
            "payment_collection_rate":  float(totals_row["payment_collection_rate"] or 0),
        },
        "monthly":       monthly,
        "top_customers": top_customers,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SO CUSTOMER LIST
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/so/customers")
def list_so_customers(db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    rows = db.execute(text("""
        SELECT company_name AS name,
               COUNT(*)     AS total_sos,
               MAX(sales_date) AS last_so
        FROM sales_orders
        GROUP BY company_name
        ORDER BY last_so DESC NULLS LAST, total_sos DESC
    """)).mappings().all()

    return [
        {
            "name":       r["name"],
            "total_sos":  int(r["total_sos"] or 0),
            "last_so":    str(r["last_so"]) if r["last_so"] else None,
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# SO CUSTOMER DEEP-DIVE
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/so/customer")
def so_customer_analytics(
    name: str = Query(..., description="Customer company_name"),
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    so_rows = db.execute(text("""
        SELECT id, invoice_number, sales_date, delivery_date,
               actual_delivery_date, status, total_amount, payment_amount
        FROM sales_orders
        WHERE company_name = :name
        ORDER BY sales_date DESC
    """), {"name": name}).mappings().all()

    if not so_rows:
        raise HTTPException(404, "No sales orders found for this customer")

    so_ids = [r["id"] for r in so_rows]

    # Items per SO
    items_by_so: dict[int, list] = {sid: [] for sid in so_ids}
    item_rows = db.execute(text("""
        SELECT sales_order_id, product_name, quantity_sold, unit_price, total_price
        FROM sales_order_items
        WHERE sales_order_id = ANY(:ids)
        ORDER BY sales_order_id, id
    """), {"ids": so_ids}).mappings().all()
    for row in item_rows:
        items_by_so[row["sales_order_id"]].append({
            "product_name":  row["product_name"],
            "quantity_sold": float(row["quantity_sold"] or 0),
            "unit_price":    float(row["unit_price"]    or 0),
            "total_price":   float(row["total_price"]   or 0),
        })

    today_str = str(datetime.now().date())
    sos_list = []
    for r in so_rows:
        sos_list.append({
            "id":                   r["id"],
            "invoice_number":       r["invoice_number"],
            "sales_date":           str(r["sales_date"])            if r["sales_date"]            else None,
            "delivery_date":        str(r["delivery_date"])         if r["delivery_date"]         else None,
            "actual_delivery_date": str(r["actual_delivery_date"])  if r["actual_delivery_date"]  else None,
            "status":               int(r["status"] or 0),
            "total_amount":         float(r["total_amount"]    or 0),
            "payment_amount":       float(r["payment_amount"])  if r["payment_amount"] is not None else None,
            "items":                items_by_so[r["id"]],
        })

    # Summary
    total_sos   = len(sos_list)
    total_revenue = sum(s["total_amount"] for s in sos_list)
    payment_received_total = sum(
        s["payment_amount"] for s in sos_list if s["payment_amount"] is not None
    )
    payment_rate_pct = round(
        100.0 * payment_received_total / total_revenue, 1
    ) if total_revenue else 0.0
    overdue = sum(
        1 for s in sos_list
        if s["delivery_date"]
        and s["actual_delivery_date"] is None
        and s["delivery_date"] < today_str
    )

    # Top 10 products
    prod_freq: dict[str, float] = {}
    for items in items_by_so.values():
        for item in items:
            pname = item["product_name"] or "Unknown"
            prod_freq[pname] = prod_freq.get(pname, 0.0) + item["quantity_sold"]
    top_products = sorted(
        [{"product_name": k, "total_qty": round(v, 2)} for k, v in prod_freq.items()],
        key=lambda x: x["total_qty"], reverse=True
    )[:10]

    # Monthly trend last 24 months
    trend_rows = db.execute(text("""
        SELECT DATE_TRUNC('month', sales_date)                                            AS mo,
               COUNT(*)                                                                   AS cnt,
               COALESCE(SUM(total_amount), 0)                                             AS value,
               COALESCE(SUM(payment_amount) FILTER (WHERE payment_amount IS NOT NULL), 0) AS payment
        FROM sales_orders
        WHERE company_name = :name
          AND sales_date >= NOW() - INTERVAL '24 months'
        GROUP BY mo ORDER BY mo ASC
    """), {"name": name}).mappings().all()

    monthly_trend = []
    for r in trend_rows:
        mo = r["mo"]
        if mo is None:
            continue
        if isinstance(mo, str):
            mo = datetime.fromisoformat(mo)
        monthly_trend.append({
            "month":   _fmt_month(mo),
            "count":   int(r["cnt"]     or 0),
            "value":   float(r["value"]   or 0),
            "payment": float(r["payment"] or 0),
        })

    return {
        "summary": {
            "total_sos":              total_sos,
            "total_revenue":          total_revenue,
            "payment_received_total": payment_received_total,
            "payment_rate_pct":       payment_rate_pct,
            "overdue":                overdue,
        },
        "sos":           sos_list,
        "top_products":  top_products,
        "monthly_trend": monthly_trend,
    }
