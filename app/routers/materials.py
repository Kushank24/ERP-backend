from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user



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
def list_materials(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    rows = db.execute(
        text(
            """
            SELECT id, name, length_weight_nos, unit, per_unit_cost, created_at, updated_at
            FROM materials ORDER BY name
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
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


@router.patch("/{material_id}")
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


@router.post("/{material_id}/convert", status_code=201)
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
