from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/finished-goods", tags=["finished-goods"])


class ReturnToInventoryBody(BaseModel):
    quantity: float = Field(gt=0)
    material_name: Optional[str] = None
    unit: Optional[str] = "Nos"
    per_unit_cost: float = Field(default=0, ge=0)
    notes: Optional[str] = None


class FinishedGoodCreate(BaseModel):
    product_name: str = Field(min_length=1)
    product_code: Optional[str] = None
    product_category: Optional[str] = None
    quantity_in_stock: float = Field(ge=0)
    work_order_id: Optional[int] = None
    work_order_number: Optional[str] = None
    party_name: Optional[str] = None
    completion_date: date
    production_cost: float = Field(default=0, ge=0)
    notes: Optional[str] = None


@router.get("")
def list_fg(
    q: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    where = "WHERE quantity_in_stock > 0"
    params: dict = {}
    if q and q.strip():
        where += (
            " AND (product_name ILIKE :q OR COALESCE(product_code,'') ILIKE :q"
            " OR COALESCE(work_order_number,'') ILIKE :q OR COALESCE(party_name,'') ILIKE :q)"
        )
        params["q"] = f"%{q.strip()}%"
    rows = db.execute(
        text(f"""
            SELECT id, product_name, product_code, product_category, quantity_in_stock,
                   work_order_id, work_order_number, party_name, completion_date,
                   production_cost, notes, created_at, updated_at
            FROM finished_goods {where}
            ORDER BY completion_date DESC, id DESC
        """),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
def create_fg(
    body: FinishedGoodCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    if body.work_order_id is not None:
        wo = db.execute(text("SELECT id FROM work_orders WHERE id = :id"), {"id": body.work_order_id}).first()
        if not wo:
            raise HTTPException(400, "work_order_id not found")

    row = db.execute(
        text(
            """
            INSERT INTO finished_goods (
              product_name, product_code, product_category, quantity_in_stock,
              work_order_id, work_order_number, party_name, completion_date, production_cost, notes
            )
            VALUES (:pn, :pc, :pcat, :qty, :woid, :won, :party, :cd, :cost, :notes)
            RETURNING id
            """
        ),
        {
            "pn": body.product_name.strip(),
            "pc": body.product_code,
            "pcat": body.product_category,
            "qty": body.quantity_in_stock,
            "woid": body.work_order_id,
            "won": body.work_order_number.strip(),
            "party": body.party_name.strip(),
            "cd": body.completion_date,
            "cost": body.production_cost,
            "notes": body.notes,
        },
    ).first()
    db.commit()
    fid = row[0]
    r = db.execute(
        text(
            """
            SELECT id, product_name, product_code, product_category, quantity_in_stock,
                   work_order_id, work_order_number, party_name, completion_date,
                   production_cost, notes, created_at, updated_at
            FROM finished_goods WHERE id = :id
            """
        ),
        {"id": fid},
    ).mappings().first()
    return dict(r)


@router.post("/{fg_id}/return-to-inventory", status_code=201)
def return_to_inventory(
    fg_id: int,
    body: ReturnToInventoryBody,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    fg = db.execute(
        text("SELECT id, product_name, quantity_in_stock FROM finished_goods WHERE id = :id FOR UPDATE"),
        {"id": fg_id},
    ).mappings().first()
    if not fg:
        raise HTTPException(404, "Finished good not found")

    current_qty = float(fg["quantity_in_stock"])
    if body.quantity > current_qty:
        raise HTTPException(
            400,
            f"Cannot return {body.quantity} — only {current_qty} in stock.",
        )

    remaining = current_qty - body.quantity
    if remaining == 0:
        db.execute(text("DELETE FROM finished_goods WHERE id = :id"), {"id": fg_id})
    else:
        db.execute(
            text("UPDATE finished_goods SET quantity_in_stock = :qty, updated_at = now() WHERE id = :id"),
            {"qty": remaining, "id": fg_id},
        )

    mat_name = (body.material_name or "").strip() or fg["product_name"]
    unit = (body.unit or "Nos").strip()

    existing = db.execute(
        text("SELECT id FROM materials WHERE LOWER(name) = LOWER(:name) LIMIT 1"),
        {"name": mat_name},
    ).first()

    if existing:
        db.execute(
            text("UPDATE materials SET length_weight_nos = length_weight_nos + :qty, updated_at = now() WHERE id = :id"),
            {"qty": body.quantity, "id": existing[0]},
        )
        mat_id = existing[0]
    else:
        mat_row = db.execute(
            text(
                """
                INSERT INTO materials (name, unit, length_weight_nos, per_unit_cost)
                VALUES (:name, :unit, :qty, :cost)
                RETURNING id
                """
            ),
            {"name": mat_name, "unit": unit, "qty": body.quantity, "cost": body.per_unit_cost},
        ).first()
        mat_id = mat_row[0]

    db.commit()

    mat_updated = db.execute(
        text("SELECT id, name, unit, length_weight_nos, per_unit_cost FROM materials WHERE id = :id"),
        {"id": mat_id},
    ).mappings().first()

    return {"deleted": remaining == 0, "material": dict(mat_updated)}
