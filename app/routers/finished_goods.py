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
    where = ""
    params: dict = {}
    if q and q.strip():
        where = (
            "WHERE (product_name ILIKE :q OR COALESCE(product_code,'') ILIKE :q"
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
