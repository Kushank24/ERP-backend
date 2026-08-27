from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

MAT_LOW = 10.0
FG_LOW = 5.0


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user

    counts = db.execute(
        text("""
            SELECT
              (SELECT COUNT(*) FROM companies)  AS companies_count,
              (SELECT COUNT(*) FROM enquiries)  AS enquiries_count,
              (SELECT COUNT(*) FROM enquiries WHERE status IN ('pending','in_progress')) AS open_enquiries_count,
              (SELECT COUNT(*) FROM offers)     AS offers_count,
              (SELECT COUNT(*) FROM offers WHERE status IN ('draft','sent')) AS active_offers_count,
              (SELECT COUNT(*) FROM materials)  AS materials_count,
              (SELECT COUNT(*) FROM materials WHERE length_weight_nos < :mth) AS low_stock_materials,
              (SELECT COALESCE(SUM(length_weight_nos * per_unit_cost), 0) FROM materials) AS inventory_value,
              (SELECT COUNT(*) FROM finished_goods) AS finished_goods_count,
              (SELECT COALESCE(SUM(quantity_in_stock), 0) FROM finished_goods) AS fg_qty
        """),
        {"mth": MAT_LOW},
    ).mappings().first()

    enquiry_status = db.execute(
        text("""
            SELECT status, COUNT(*) AS cnt
            FROM enquiries
            GROUP BY status
        """)
    ).mappings().all()

    offer_status = db.execute(
        text("""
            SELECT status, COUNT(*) AS cnt
            FROM offers
            GROUP BY status
        """)
    ).mappings().all()

    wo_counts = db.execute(
        text("""
            SELECT
              COUNT(*) FILTER (WHERE status = 'in-progress') AS in_progress,
              COUNT(*) FILTER (WHERE status = 'completed') AS completed,
              COUNT(*) AS total
            FROM work_orders
        """)
    ).mappings().first()

    low_mat_sample = db.execute(
        text("""
            SELECT id, name, length_weight_nos, unit
            FROM materials
            WHERE length_weight_nos < :mth
            ORDER BY length_weight_nos ASC
            LIMIT 5
        """),
        {"mth": MAT_LOW},
    ).mappings().all()

    low_fg_count = db.execute(
        text("SELECT COUNT(*) FROM finished_goods WHERE quantity_in_stock > 0 AND quantity_in_stock < :th"),
        {"th": FG_LOW},
    ).scalar()

    recent_enquiries = db.execute(
        text("""
            SELECT e.enquiry_number, c.name AS company_name, e.status, e.priority, e.enquiry_date
            FROM enquiries e
            LEFT JOIN companies c ON c.id = e.company_id
            ORDER BY e.enquiry_date DESC, e.id DESC
            LIMIT 5
        """)
    ).mappings().all()

    recent_offers = db.execute(
        text("""
            SELECT o.offer_number, c.name AS company_name, o.status, o.total_amount, o.offer_date
            FROM offers o
            LEFT JOIN companies c ON c.id = o.company_id
            ORDER BY o.offer_date DESC, o.id DESC
            LIMIT 5
        """)
    ).mappings().all()

    return {
        "companies_count": int(counts["companies_count"] or 0),
        "enquiries_count": int(counts["enquiries_count"] or 0),
        "open_enquiries_count": int(counts["open_enquiries_count"] or 0),
        "offers_count": int(counts["offers_count"] or 0),
        "active_offers_count": int(counts["active_offers_count"] or 0),
        "materials_count": int(counts["materials_count"] or 0),
        "finished_goods_count": int(counts["finished_goods_count"] or 0),
        "low_stock_materials_count": int(counts["low_stock_materials"] or 0),
        "low_stock_finished_goods_count": int(low_fg_count or 0),
        "total_inventory_value": float(counts["inventory_value"] or 0),
        "total_fg_quantity": float(counts["fg_qty"] or 0),
        "work_orders": {
            "total": int(wo_counts["total"] or 0),
            "in_progress": int(wo_counts["in_progress"] or 0),
            "completed": int(wo_counts["completed"] or 0),
        },
        "enquiry_by_status": {r["status"]: int(r["cnt"]) for r in enquiry_status},
        "offer_by_status": {r["status"]: int(r["cnt"]) for r in offer_status},
        "recent_enquiries": [dict(r) for r in recent_enquiries],
        "recent_offers": [dict(r) for r in recent_offers],
        "low_stock_materials_sample": [dict(r) for r in low_mat_sample],
    }
