from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/enquiries", tags=["enquiries"])

VALID_STATUSES = {"pending", "in_progress", "offer_sent", "completed", "cancelled"}
VALID_PRIORITIES = {"low", "medium", "high"}


class EnquiryItemIn(BaseModel):
    product_id: Optional[int] = None
    product_name: Optional[str] = None
    quantity: int = Field(default=1, ge=1)
    specifications: Optional[str] = None


class EnquiryCreate(BaseModel):
    company_id: Optional[int] = None
    enquiry_date: date = Field(default_factory=date.today)
    status: str = "pending"
    priority: str = "medium"
    notes: Optional[str] = None
    reference_number: Optional[str] = None
    items: List[EnquiryItemIn] = Field(default_factory=list)


class EnquiryUpdate(BaseModel):
    company_id: Optional[int] = None
    enquiry_date: date
    status: str
    priority: str
    notes: Optional[str] = None
    reference_number: Optional[str] = None
    items: List[EnquiryItemIn] = Field(default_factory=list)


def _next_enquiry_number(db: Session) -> str:
    year = date.today().year
    row = db.execute(
        text("SELECT COUNT(*) AS cnt FROM enquiries WHERE enquiry_number LIKE :prefix"),
        {"prefix": f"ENQ-{year}-%"},
    ).mappings().first()
    seq = (row["cnt"] or 0) + 1
    return f"ENQ-{year}-{seq:04d}"


def _serialize(db: Session, enq_id: int) -> dict:
    enq = db.execute(
        text("""
            SELECT e.*, c.name AS company_name
            FROM enquiries e
            LEFT JOIN companies c ON c.id = e.company_id
            WHERE e.id = :id
        """),
        {"id": enq_id},
    ).mappings().first()
    if not enq:
        raise HTTPException(status_code=404, detail="Enquiry not found")

    items = db.execute(
        text("""
            SELECT ei.*, p.name AS product_name_resolved
            FROM enquiry_items ei
            LEFT JOIN products p ON p.id = ei.product_id
            WHERE ei.enquiry_id = :id
            ORDER BY ei.id
        """),
        {"id": enq_id},
    ).mappings().all()

    return {**dict(enq), "items": [dict(i) for i in items]}


@router.get("")
def list_enquiries(
    status: Optional[str] = None,
    company_id: Optional[int] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    where = []
    params: dict = {}
    if status:
        where.append("e.status = :status")
        params["status"] = status
    if company_id:
        where.append("e.company_id = :company_id")
        params["company_id"] = company_id
    if q:
        where.append("(c.name ILIKE :q OR e.enquiry_number ILIKE :q OR e.reference_number ILIKE :q)")
        params["q"] = f"%{q}%"

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    base = f"""
        FROM enquiries e
        LEFT JOIN companies c ON c.id = e.company_id
        {clause}
    """
    total = db.execute(text(f"SELECT COUNT(*) {base}"), params).scalar()
    params["limit"] = limit
    params["offset"] = offset
    rows = db.execute(
        text(f"""
            SELECT e.*, c.name AS company_name {base}
            ORDER BY e.enquiry_date DESC, e.id DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    ).mappings().all()
    return {"data": [dict(r) for r in rows], "total": total}


@router.post("", status_code=201)
def create_enquiry(
    body: EnquiryCreate,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Choose from: {VALID_STATUSES}")
    if body.priority not in VALID_PRIORITIES:
        raise HTTPException(400, f"Invalid priority. Choose from: {VALID_PRIORITIES}")

    enq_num = _next_enquiry_number(db)
    row = db.execute(
        text("""
            INSERT INTO enquiries
                (company_id, enquiry_number, enquiry_date, status, priority, notes, reference_number)
            VALUES
                (:company_id, :enquiry_number, :enquiry_date, :status, :priority, :notes, :reference_number)
            RETURNING id
        """),
        {
            "company_id": body.company_id,
            "enquiry_number": enq_num,
            "enquiry_date": body.enquiry_date,
            "status": body.status,
            "priority": body.priority,
            "notes": body.notes,
            "reference_number": body.reference_number,
        },
    ).mappings().first()
    enq_id = row["id"]

    for item in body.items:
        db.execute(
            text("""
                INSERT INTO enquiry_items (enquiry_id, product_id, product_name, quantity, specifications)
                VALUES (:enquiry_id, :product_id, :product_name, :quantity, :specifications)
            """),
            {
                "enquiry_id": enq_id,
                "product_id": item.product_id,
                "product_name": item.product_name,
                "quantity": item.quantity,
                "specifications": item.specifications,
            },
        )

    db.commit()
    return _serialize(db, enq_id)


@router.get("/{enquiry_id}")
def get_enquiry(
    enquiry_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    return _serialize(db, enquiry_id)


@router.put("/{enquiry_id}")
def update_enquiry(
    enquiry_id: int,
    body: EnquiryUpdate,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    _serialize(db, enquiry_id)  # 404 guard
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status")
    if body.priority not in VALID_PRIORITIES:
        raise HTTPException(400, f"Invalid priority")

    db.execute(
        text("""
            UPDATE enquiries
            SET company_id = :company_id, enquiry_date = :enquiry_date,
                status = :status, priority = :priority,
                notes = :notes, reference_number = :reference_number,
                updated_at = NOW()
            WHERE id = :id
        """),
        {
            "id": enquiry_id,
            "company_id": body.company_id,
            "enquiry_date": body.enquiry_date,
            "status": body.status,
            "priority": body.priority,
            "notes": body.notes,
            "reference_number": body.reference_number,
        },
    )

    db.execute(text("DELETE FROM enquiry_items WHERE enquiry_id = :id"), {"id": enquiry_id})
    for item in body.items:
        db.execute(
            text("""
                INSERT INTO enquiry_items (enquiry_id, product_id, product_name, quantity, specifications)
                VALUES (:enquiry_id, :product_id, :product_name, :quantity, :specifications)
            """),
            {
                "enquiry_id": enquiry_id,
                "product_id": item.product_id,
                "product_name": item.product_name,
                "quantity": item.quantity,
                "specifications": item.specifications,
            },
        )

    db.commit()
    return _serialize(db, enquiry_id)


@router.delete("/{enquiry_id}", status_code=204)
def delete_enquiry(
    enquiry_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    _serialize(db, enquiry_id)  # 404 guard
    db.execute(text("DELETE FROM enquiries WHERE id = :id"), {"id": enquiry_id})
    db.commit()
