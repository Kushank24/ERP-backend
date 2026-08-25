from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..pdf_service import PDFGenerationService

_pdf_svc = PDFGenerationService()

router = APIRouter(prefix="/work-orders", tags=["work-orders"])


class WoProductIn(BaseModel):
    product_id: int = Field(gt=0)
    quantity: float = Field(gt=0)


class WorkOrderCreate(BaseModel):
    work_order_number: str = Field(min_length=1)
    po_number: str = Field(min_length=1)
    po_date: date
    party_name: str = Field(min_length=1)
    creation_date: Optional[date] = None
    delivery_date: Optional[date] = None
    status: str = Field(default="in-progress")
    products: List[WoProductIn] = []


def _load_wo(db: Session, wo_id: int) -> dict:
    head = db.execute(
        text(
            """
            SELECT id, work_order_number, po_number, po_date, party_name, creation_date, delivery_date, status,
                   created_at, updated_at
            FROM work_orders WHERE id = :id
            """
        ),
        {"id": wo_id},
    ).mappings().first()
    if not head:
        raise HTTPException(404, "Work order not found")
    prows = db.execute(
        text(
            """
            SELECT w.product_id, w.quantity, p.name AS product_name
            FROM work_order_products w
            JOIN products p ON p.id = w.product_id
            WHERE w.work_order_id = :id
            """
        ),
        {"id": wo_id},
    ).mappings().all()
    d = dict(head)
    d["products"] = [dict(x) for x in prows]
    return d


@router.get("")
def list_wos(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    rows = db.execute(
        text(
            """
            SELECT id, work_order_number, po_number, party_name, status, delivery_date, creation_date
            FROM work_orders ORDER BY creation_date DESC, id DESC
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{wo_id}")
def get_wo(wo_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    return _load_wo(db, wo_id)

@router.get("/parties/list")
def list_parties(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    rows = db.execute(
        text("SELECT DISTINCT ON (party_name) party_name FROM work_orders WHERE party_name IS NOT NULL AND party_name != '' ORDER BY party_name, created_at DESC")
    ).mappings().all()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
def create_wo(
    body: WorkOrderCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    dup = db.execute(
        text("SELECT id FROM work_orders WHERE work_order_number = :n"),
        {"n": body.work_order_number.strip()},
    ).first()
    if dup:
        raise HTTPException(400, "Work order number already exists")

    cre = body.creation_date or date.today()
    row = db.execute(
        text(
            """
            INSERT INTO work_orders (
              work_order_number, po_number, po_date, party_name, creation_date, delivery_date, status
            )
            VALUES (:won, :pon, :pod, :party, :cd, :dd, :st)
            RETURNING id
            """
        ),
        {
            "won": body.work_order_number.strip(),
            "pon": body.po_number.strip(),
            "pod": body.po_date,
            "party": body.party_name.strip(),
            "cd": cre,
            "dd": body.delivery_date,
            "st": body.status,
        },
    ).first()
    wid = row[0]

    seen: set[int] = set()
    for p in body.products:
        if p.product_id in seen:
            db.rollback()
            raise HTTPException(400, "Duplicate product in work order")
        seen.add(p.product_id)
        pr = db.execute(text("SELECT id FROM products WHERE id = :id"), {"id": p.product_id}).first()
        if not pr:
            db.rollback()
            raise HTTPException(400, f"Product {p.product_id} not found")
        db.execute(
            text(
                """
                INSERT INTO work_order_products (work_order_id, product_id, quantity)
                VALUES (:wid, :pid, :qty)
                """
            ),
            {"wid": wid, "pid": p.product_id, "qty": p.quantity},
        )

    db.commit()
    return _load_wo(db, wid)


class StatusBody(BaseModel):
    status: Literal["in-progress", "completed"]


@router.patch("/{wo_id}/status")
def patch_status(
    wo_id: int,
    body: StatusBody,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    current_status = db.execute(
        text("SELECT status FROM work_orders WHERE id = :id FOR UPDATE"), {"id": wo_id}
    ).scalar()
    
    if current_status is None:
        raise HTTPException(404, "Work order not found")

    if current_status != "completed" and body.status == "completed":
        wo = _load_wo(db, wo_id)
        for p in wo["products"]:
            # Reduce materials based on BoQ
            boq_lines = db.execute(
                text(
                    """
                    SELECT b.name, b.total_quantity_consumed 
                    FROM bill_of_quantities b
                    JOIN product_bill_of_quantity_relations r ON r.bill_of_quantity_id = b.id
                    WHERE r.product_id = :pid
                    """
                ),
                {"pid": p["product_id"]}
            ).mappings().all()

            for b in boq_lines:
                deduction = float(b["total_quantity_consumed"]) * float(p["quantity"])
                db.execute(
                    text("UPDATE materials SET length_weight_nos = length_weight_nos - :deduct WHERE name = :mname"),
                    {"deduct": deduction, "mname": b["name"]}
                )

            # Insert into finished goods
            p_info = db.execute(
                text("SELECT name, product_code, category FROM products WHERE id = :pid"),
                {"pid": p["product_id"]}
            ).mappings().first()

            if p_info:
                db.execute(
                    text(
                        """
                        INSERT INTO finished_goods (
                            product_name, product_code, product_category, quantity_in_stock,
                            work_order_id, work_order_number, party_name, completion_date
                        ) VALUES (
                            :pname, :pcode, :pcat, :qty, :woid, :wonum, :party, CURRENT_DATE
                        )
                        """
                    ),
                    {
                        "pname": p_info["name"],
                        "pcode": p_info["product_code"],
                        "pcat": p_info["category"],
                        "qty": p["quantity"],
                        "woid": wo_id,
                        "wonum": wo["work_order_number"],
                        "party": wo["party_name"]
                    }
                )

    r = db.execute(
        text("UPDATE work_orders SET status = :s, updated_at = now() WHERE id = :id RETURNING id"),
        {"s": body.status, "id": wo_id},
    ).first()
    db.commit()
    return _load_wo(db, wo_id)


@router.get("/{wo_id}/pdf")
def download_wo_pdf(
    wo_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    wo = _load_wo(db, wo_id)

    # Compute material requirements from each product's BOQ lines.
    materials: dict = {}
    for p in wo["products"]:
        boq_lines = db.execute(
            text(
                """
                SELECT b.name, b.units, b.quantity AS qty_per_unit, b.total_quantity_consumed
                FROM bill_of_quantities b
                JOIN product_bill_of_quantity_relations r ON r.bill_of_quantity_id = b.id
                WHERE r.product_id = :pid
                """
            ),
            {"pid": p["product_id"]},
        ).mappings().all()

        for b in boq_lines:
            key = b["name"]
            qty_per = float(b["qty_per_unit"])
            total = float(b["total_quantity_consumed"]) * float(p["quantity"])
            if key in materials:
                materials[key]["total_required"] = round(
                    materials[key]["total_required"] + total, 4
                )
            else:
                materials[key] = {
                    "unit": b["units"],
                    "quantity_per_unit": round(qty_per, 4),
                    "total_required": round(total, 4),
                }

    pdf_data = {
        "work_order_number": wo["work_order_number"],
        "po_number": wo.get("po_number") or "",
        "po_date": str(wo.get("po_date") or ""),
        "party_name": wo.get("party_name") or "",
        "delivery_date": str(wo.get("delivery_date") or ""),
        "products": [
            {
                "name": p["product_name"],
                "quantity": p["quantity"],
                "product_code": "",
            }
            for p in wo["products"]
        ],
        "materials": materials,
    }

    import re
    def _safe(s: str) -> str:
        return re.sub(r"[^\w\-]", "_", s).strip("_")

    wo_num = _safe(wo["work_order_number"])
    party = _safe(wo.get("party_name") or "Unknown")
    wo_date = _safe(str(wo.get("creation_date") or ""))
    filename = f"{wo_num}-{party}-{wo_date}.pdf"

    buf = _pdf_svc.generate_work_order_pdf(pdf_data)
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
