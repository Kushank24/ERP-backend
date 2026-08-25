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
        text(
            """
            SELECT
              (SELECT COUNT(*) FROM materials) AS materials_count,
              (SELECT COUNT(*) FROM products) AS products_count,
              (SELECT COUNT(*) FROM purchase_orders) AS purchase_orders_count,
              (SELECT COUNT(*) FROM finished_goods) AS finished_goods_count,
              (SELECT COUNT(*) FROM materials WHERE length_weight_nos < :mth) AS low_stock_materials,
              (SELECT COALESCE(SUM(length_weight_nos * per_unit_cost), 0) FROM materials) AS inventory_value,
              (SELECT COALESCE(SUM(total_amount), 0) FROM purchase_orders) AS po_value,
              (SELECT COALESCE(SUM(quantity_in_stock), 0) FROM finished_goods) AS fg_qty
            """
        ),
        {"mth": MAT_LOW},
    ).mappings().first()

    recent_pos = db.execute(
        text(
            """
            SELECT p.id, p.purchase_number, s.name AS supplier_name, p.total_amount, p.created_at
            FROM purchase_orders p
            LEFT JOIN suppliers s ON s.id = p.supplier_id
            ORDER BY p.created_at DESC NULLS LAST
            LIMIT 5
            """
        )
    ).mappings().all()

    recent_fg = db.execute(
        text(
            """
            SELECT id, product_name, party_name, quantity_in_stock, completion_date
            FROM finished_goods
            ORDER BY completion_date DESC, created_at DESC
            LIMIT 5
            """
        )
    ).mappings().all()

    low_mat_sample = db.execute(
        text(
            """
            SELECT id, name, length_weight_nos, unit
            FROM materials
            WHERE length_weight_nos < :mth
            ORDER BY length_weight_nos ASC
            LIMIT 5
            """
        ),
        {"mth": MAT_LOW},
    ).mappings().all()

    wo_counts = db.execute(
        text(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = 'in-progress') AS in_progress,
              COUNT(*) FILTER (WHERE status = 'completed') AS completed,
              COUNT(*) AS total
            FROM work_orders
            """
        )
    ).mappings().first()

    low_fg_count = db.execute(
        text("SELECT COUNT(*) FROM finished_goods WHERE quantity_in_stock > 0 AND quantity_in_stock < :th"),
        {"th": FG_LOW},
    ).scalar()

    return {
        "materials_count": int(counts["materials_count"] or 0),
        "products_count": int(counts["products_count"] or 0),
        "purchase_orders_count": int(counts["purchase_orders_count"] or 0),
        "finished_goods_count": int(counts["finished_goods_count"] or 0),
        "low_stock_materials_count": int(counts["low_stock_materials"] or 0),
        "low_stock_finished_goods_count": int(low_fg_count or 0),
        "total_inventory_value": float(counts["inventory_value"] or 0),
        "total_po_value": float(counts["po_value"] or 0),
        "total_fg_quantity": float(counts["fg_qty"] or 0),
        "work_orders": {
            "total": int(wo_counts["total"] or 0),
            "in_progress": int(wo_counts["in_progress"] or 0),
            "completed": int(wo_counts["completed"] or 0),
        },
        "recent_purchase_orders": [dict(r) for r in recent_pos],
        "recent_finished_goods": [dict(r) for r in recent_fg],
        "low_stock_materials_sample": [dict(r) for r in low_mat_sample],
    }
