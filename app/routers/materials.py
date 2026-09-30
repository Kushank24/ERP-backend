from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user, require_module



router = APIRouter(prefix="/materials", tags=["materials"])


class MaterialCreate(BaseModel):
    name: str = Field(min_length=1)
    length_weight_nos: float = Field(ge=0)
    unit: str = Field(min_length=1)
    per_unit_cost: float = Field(ge=0, default=0)


class MaterialPatch(BaseModel):
    name: Optional[str] = None
    length_weight_nos: Optional[float] = Field(default=None, ge=0)
    unit: Optional[str] = None
    per_unit_cost: Optional[float] = Field(default=None, ge=0)


class ConvertToFGBody(BaseModel):
    quantity: float = Field(gt=0)
    product_name: str = Field(min_length=1)
    product_code: Optional[str] = None
    product_category: Optional[str] = None
    notes: Optional[str] = None


@router.get("")
def list_materials(
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user

    # Aggregates over ALL materials (independent of search filter)
    stats = db.execute(text("""
        SELECT
            COUNT(*)::int                                          AS total_count,
            COALESCE(SUM(length_weight_nos * per_unit_cost), 0)   AS total_value,
            COUNT(*) FILTER (WHERE length_weight_nos < 10)::int   AS low_stock_count
        FROM materials
    """)).mappings().first()

    where = ""
    params: dict = {}
    if q and q.strip():
        where = "WHERE name ILIKE :q"
        params["q"] = f"%{q.strip()}%"

    total = db.execute(
        text(f"SELECT COUNT(*) FROM materials {where}"), params
    ).scalar_one()

    params["limit"] = limit
    params["offset"] = offset
    rows = db.execute(
        text(f"""
            SELECT id, name, length_weight_nos, unit, per_unit_cost, created_at, updated_at
            FROM materials {where}
            ORDER BY name
            LIMIT :limit OFFSET :offset
        """),
        params,
    ).mappings().all()

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "total_count": stats["total_count"],
        "total_value": float(stats["total_value"]),
        "low_stock_count": stats["low_stock_count"],
    }


@router.post("", status_code=201, dependencies=[Depends(require_module("inventory"))])
def create_material(
    body: MaterialCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    res = db.execute(
        text(
            """
            INSERT INTO materials (name, length_weight_nos, unit, per_unit_cost)
            VALUES (:name, :lwn, :unit, :cost)
            ON CONFLICT (name) DO UPDATE
                SET length_weight_nos = materials.length_weight_nos + EXCLUDED.length_weight_nos,
                    per_unit_cost     = EXCLUDED.per_unit_cost,
                    updated_at        = now()
            RETURNING id, name, length_weight_nos, unit, per_unit_cost, created_at, updated_at
            """
        ),
        {
            "name": body.name.strip(),
            "lwn": body.length_weight_nos,
            "unit": body.unit.strip(),
            "cost": body.per_unit_cost,
        },
    ).mappings().first()
    db.commit()
    return dict(res)


@router.patch("/{material_id}", dependencies=[Depends(require_module("inventory"))])
def patch_material(
    material_id: int,
    body: MaterialPatch,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    row = db.execute(text("SELECT id FROM materials WHERE id = :id"), {"id": material_id}).first()
    if not row:
        raise HTTPException(404, "Material not found")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        db.execute(text("SELECT 1"))
        r = db.execute(
            text(
                "SELECT id, name, length_weight_nos, unit, per_unit_cost, created_at, updated_at FROM materials WHERE id = :id"
            ),
            {"id": material_id},
        ).mappings().first()
        return dict(r)
    sets = []
    params = {"id": material_id}
    for k, v in fields.items():
        sets.append(f"{k} = :{k}")
        params[k] = v
    sets.append("updated_at = now()")
    db.execute(
        text(f"UPDATE materials SET {', '.join(sets)} WHERE id = :id"),
        params,
    )
    db.commit()
    r = db.execute(
        text(
            "SELECT id, name, length_weight_nos, unit, per_unit_cost, created_at, updated_at FROM materials WHERE id = :id"
        ),
        {"id": material_id},
    ).mappings().first()
    return dict(r)


@router.get("/{material_id}/history")
def get_material_history(
    material_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    mat = db.execute(
        text("SELECT id, name, unit FROM materials WHERE id = :id"),
        {"id": material_id},
    ).mappings().first()
    if not mat:
        raise HTTPException(404, "Material not found")

    purchases = db.execute(
        text("""
            SELECT
                po.id            AS po_id,
                po.purchase_number,
                po.purchase_date,
                s.name           AS supplier_name,
                s.location       AS supplier_location,
                s.contact        AS supplier_contact,
                pol.length_weight_nos AS quantity_ordered,
                pol.delivered_qty     AS quantity_delivered,
                pol.per_unit_cost,
                pol.unit,
                pol.comment
            FROM purchase_order_lines pol
            JOIN purchase_orders po ON po.id = pol.purchase_order_id
            LEFT JOIN suppliers s   ON s.id  = po.supplier_id
            WHERE LOWER(TRIM(pol.material_name)) = LOWER(TRIM(:name))
            ORDER BY po.purchase_date DESC NULLS LAST, po.id DESC
        """),
        {"name": mat["name"]},
    ).mappings().all()

    usages = db.execute(
        text("""
            SELECT
                wo.id                 AS wo_id,
                wo.work_order_number,
                wo.party_name,
                wo.updated_at         AS completed_at,
                wo.delivery_date,
                SUM(b.total_quantity_consumed * wop.quantity) AS quantity_consumed,
                b.units               AS boq_unit
            FROM work_orders wo
            JOIN work_order_products wop
                ON wop.work_order_id = wo.id
            JOIN product_bill_of_quantity_relations pbr
                ON pbr.product_id = wop.product_id
            JOIN bill_of_quantities b
                ON b.id = pbr.bill_of_quantity_id
            WHERE wo.status = 'completed'
              AND LOWER(TRIM(b.name)) = LOWER(TRIM(:name))
            GROUP BY wo.id, wo.work_order_number, wo.party_name,
                     wo.updated_at, wo.delivery_date, b.units
            ORDER BY wo.updated_at DESC NULLS LAST
        """),
        {"name": mat["name"]},
    ).mappings().all()

    return {
        "material": dict(mat),
        "purchases": [dict(r) for r in purchases],
        "usages": [dict(r) for r in usages],
    }


@router.post("/{material_id}/convert", status_code=201, dependencies=[Depends(require_module("inventory"))])
def convert_to_finished_good(
    material_id: int,
    body: ConvertToFGBody,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    mat = db.execute(
        text("SELECT id, name, length_weight_nos, unit FROM materials WHERE id = :id FOR UPDATE"),
        {"id": material_id},
    ).mappings().first()
    if not mat:
        raise HTTPException(404, "Material not found")

    current_qty = float(mat["length_weight_nos"])
    if body.quantity > current_qty:
        raise HTTPException(
            400,
            f"Cannot convert {body.quantity} — only {current_qty} {mat['unit']} in stock.",
        )

    db.execute(
        text("UPDATE materials SET length_weight_nos = length_weight_nos - :qty, updated_at = now() WHERE id = :id"),
        {"qty": body.quantity, "id": material_id},
    )

    fg_row = db.execute(
        text(
            """
            INSERT INTO finished_goods (
                product_name, product_code, product_category,
                quantity_in_stock, completion_date, notes
            )
            VALUES (:pn, :pc, :pcat, :qty, CURRENT_DATE, :notes)
            RETURNING id, product_name, product_code, product_category,
                      quantity_in_stock, completion_date, notes, created_at
            """
        ),
        {
            "pn": body.product_name.strip(),
            "pc": body.product_code,
            "pcat": body.product_category,
            "qty": body.quantity,
            "notes": body.notes,
        },
    ).mappings().first()

    db.commit()

    mat_updated = db.execute(
        text("SELECT id, name, length_weight_nos, unit, per_unit_cost, created_at, updated_at FROM materials WHERE id = :id"),
        {"id": material_id},
    ).mappings().first()

    return {"material": dict(mat_updated), "finished_good": dict(fg_row)}
