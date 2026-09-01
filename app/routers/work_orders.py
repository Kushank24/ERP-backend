from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
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
    po_number: Optional[str] = None
    po_date: Optional[date] = None
    party_name: Optional[str] = None
    creation_date: Optional[date] = None
    delivery_date: Optional[date] = None
    status: str = Field(default="in-progress")
    remarks: Optional[str] = None
    products: List[WoProductIn] = []


class WorkOrderUpdate(BaseModel):
    work_order_number: str = Field(min_length=1)
    po_number: Optional[str] = None
    po_date: Optional[date] = None
    party_name: Optional[str] = None
    creation_date: Optional[date] = None
    delivery_date: Optional[date] = None
    remarks: Optional[str] = None
    products: List[WoProductIn] = []


def _load_wo(db: Session, wo_id: int) -> dict:
    head = db.execute(
        text(
            """
            SELECT id, work_order_number, po_number, po_date, party_name, creation_date, delivery_date, status,
                   remarks, created_at, updated_at
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
            SELECT w.product_id, w.quantity, p.name AS product_name,
                   COALESCE(SUM(i.quantity_issued), 0) AS issued_qty
            FROM work_order_products w
            JOIN products p ON p.id = w.product_id
            LEFT JOIN work_order_product_issues i
                ON i.work_order_id = w.work_order_id AND i.product_id = w.product_id
            WHERE w.work_order_id = :id
            GROUP BY w.product_id, w.quantity, p.name
            """
        ),
        {"id": wo_id},
    ).mappings().all()
    d = dict(head)
    products = []
    for x in prows:
        row = dict(x)
        row["issued_qty"] = float(row["issued_qty"])
        row["remaining_qty"] = max(0.0, float(row["quantity"]) - row["issued_qty"])
        products.append(row)
    d["products"] = products
    return d


def _compute_materials(db: Session, products: list) -> list:
    mats_dict: dict = {}
    for p in products:
        boq_lines = db.execute(
            text(
                """
                SELECT b.name, b.section_size, b.units, b.quantity AS qty_per_unit, b.total_quantity_consumed
                FROM bill_of_quantities b
                JOIN product_bill_of_quantity_relations r ON r.bill_of_quantity_id = b.id
                WHERE r.product_id = :pid
                """
            ),
            {"pid": p["product_id"]},
        ).mappings().all()
        for b in boq_lines:
            ss = float(b["section_size"] or 0)
            key = (b["name"], ss)
            total = float(b["total_quantity_consumed"]) * float(p["quantity"])
            if key in mats_dict:
                mats_dict[key]["total_required"] = round(mats_dict[key]["total_required"] + total, 4)
            else:
                mats_dict[key] = {
                    "name": b["name"],
                    "section_size": ss,
                    "unit": b["units"],
                    "quantity_per_unit": round(float(b["qty_per_unit"]), 4),
                    "total_required": round(total, 4),
                }
    return list(mats_dict.values())


@router.get("")
def list_wos(
    q: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    conditions = []
    params: dict = {}
    if q and q.strip():
        conditions.append(
            "(work_order_number ILIKE :q OR COALESCE(po_number,'') ILIKE :q OR COALESCE(party_name,'') ILIKE :q)"
        )
        params["q"] = f"%{q.strip()}%"
    if status and status.strip():
        conditions.append("status = :status")
        params["status"] = status.strip()
    if date_from:
        conditions.append("creation_date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        conditions.append("creation_date <= :date_to")
        params["date_to"] = date_to
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    base = f"FROM work_orders {where}"
    total = db.execute(text(f"SELECT COUNT(*) {base}"), params).scalar()
    params["limit"] = limit
    params["offset"] = offset
    rows = db.execute(
        text(f"""
            SELECT id, work_order_number, po_number, party_name, status, delivery_date, creation_date
            {base}
            ORDER BY creation_date DESC, id DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    ).mappings().all()
    return {"data": [dict(r) for r in rows], "total": total}


@router.get("/{wo_id}")
def get_wo(wo_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    return _load_wo(db, wo_id)

@router.get("/{wo_id}/materials")
def get_wo_materials(wo_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    wo = _load_wo(db, wo_id)
    return _compute_materials(db, wo["products"])

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
              work_order_number, po_number, po_date, party_name, creation_date, delivery_date, status, remarks
            )
            VALUES (:won, :pon, :pod, :party, :cd, :dd, :st, :remarks)
            RETURNING id
            """
        ),
        {
            "won": body.work_order_number.strip(),
            "pon": (body.po_number or "").strip() or None,
            "pod": body.po_date,
            "party": (body.party_name or "").strip() or None,
            "cd": cre,
            "dd": body.delivery_date,
            "st": body.status,
            "remarks": body.remarks,
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


@router.patch("/{wo_id}")
def update_wo(
    wo_id: int,
    body: WorkOrderUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    current = db.execute(
        text("SELECT id, status FROM work_orders WHERE id = :id"), {"wo_id": wo_id, "id": wo_id}
    ).mappings().first()
    if not current:
        raise HTTPException(404, "Work order not found")

    if current["status"] == "completed" and body.products:
        # Only block product changes on completed WOs; header/remarks are fine
        existing_products = db.execute(
            text("SELECT product_id, quantity FROM work_order_products WHERE work_order_id = :id"),
            {"id": wo_id},
        ).mappings().all()
        existing_set = {(r["product_id"], float(r["quantity"])) for r in existing_products}
        new_set = {(p.product_id, float(p.quantity)) for p in body.products}
        if existing_set != new_set:
            raise HTTPException(
                400,
                "Cannot change products on a completed work order — inventory has already been updated.",
            )

    db.execute(
        text(
            "UPDATE work_orders SET work_order_number=:won, po_number=:pon, po_date=:pod, "
            "party_name=:party, creation_date=:cd, delivery_date=:dd, remarks=:remarks, updated_at=now() "
            "WHERE id=:id"
        ),
        {
            "won": body.work_order_number.strip(),
            "pon": (body.po_number or "").strip() or None,
            "pod": body.po_date,
            "party": (body.party_name or "").strip() or None,
            "cd": body.creation_date,
            "dd": body.delivery_date,
            "remarks": body.remarks,
            "id": wo_id,
        },
    )

    if current["status"] != "completed":
        db.execute(text("DELETE FROM work_order_products WHERE work_order_id = :id"), {"id": wo_id})
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
                text("INSERT INTO work_order_products (work_order_id, product_id, quantity) VALUES (:wid, :pid, :qty)"),
                {"wid": wo_id, "pid": p.product_id, "qty": p.quantity},
            )

    db.commit()
    return _load_wo(db, wo_id)


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


class ProductIssueItem(BaseModel):
    product_id: int
    product_name: str = Field(min_length=1)
    quantity: float = Field(gt=0)
    notes: Optional[str] = None


class ProductIssueBody(BaseModel):
    items: List[ProductIssueItem] = Field(min_length=1)


@router.post("/{wo_id}/issue-products", status_code=201)
def issue_products(
    wo_id: int,
    body: ProductIssueBody,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    wo_row = db.execute(
        text("SELECT id, work_order_number, party_name FROM work_orders WHERE id = :id FOR UPDATE"),
        {"id": wo_id},
    ).mappings().first()
    if not wo_row:
        raise HTTPException(404, "Work order not found")

    # Current issued quantities per product
    wp_rows = {
        r["product_id"]: dict(r)
        for r in db.execute(
            text(
                """
                SELECT w.product_id, w.quantity,
                       COALESCE(SUM(i.quantity_issued), 0) AS issued_qty
                FROM work_order_products w
                LEFT JOIN work_order_product_issues i
                    ON i.work_order_id = w.work_order_id AND i.product_id = w.product_id
                WHERE w.work_order_id = :id
                GROUP BY w.product_id, w.quantity
                """
            ),
            {"id": wo_id},
        ).mappings().all()
    }

    for item in body.items:
        if item.product_id not in wp_rows:
            raise HTTPException(400, f"Product id {item.product_id} is not part of this work order.")
        prod = wp_rows[item.product_id]
        remaining = float(prod["quantity"]) - float(prod["issued_qty"])
        if item.quantity > remaining + 1e-9:
            raise HTTPException(
                400,
                f"Cannot issue {item.quantity} of '{item.product_name}' — only {remaining:.4g} remaining.",
            )

        db.execute(
            text(
                """
                INSERT INTO work_order_product_issues
                    (work_order_id, product_id, product_name, quantity_issued, notes)
                VALUES (:wid, :pid, :pname, :qty, :notes)
                """
            ),
            {
                "wid": wo_id,
                "pid": item.product_id,
                "pname": item.product_name,
                "qty": item.quantity,
                "notes": item.notes,
            },
        )

        p_info = db.execute(
            text("SELECT name, product_code, category FROM products WHERE id = :pid"),
            {"pid": item.product_id},
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
                    "qty": item.quantity,
                    "woid": wo_id,
                    "wonum": wo_row["work_order_number"],
                    "party": wo_row["party_name"],
                },
            )

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
        "materials": _compute_materials(db, wo["products"]),
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
