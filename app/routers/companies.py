from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/companies", tags=["companies"])


class CompanyIn(BaseModel):
    name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    gstin: Optional[str] = None


def _row(db: Session, company_id: int) -> dict:
    row = db.execute(
        text("SELECT * FROM companies WHERE id = :id"),
        {"id": company_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Company not found")
    return dict(row)


@router.get("")
def list_companies(
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    where = ""
    params: dict = {}
    if q:
        where = " WHERE name ILIKE :q OR contact_person ILIKE :q OR email ILIKE :q"
        params["q"] = f"%{q}%"
    total = db.execute(text(f"SELECT COUNT(*) FROM companies{where}"), params).scalar()
    params["limit"] = limit
    params["offset"] = offset
    rows = db.execute(
        text(f"SELECT * FROM companies{where} ORDER BY name LIMIT :limit OFFSET :offset"),
        params,
    ).mappings().all()
    return {"data": [dict(r) for r in rows], "total": total}


@router.post("", status_code=201)
def create_company(
    body: CompanyIn,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    row = db.execute(
        text("""
            INSERT INTO companies (name, contact_person, email, phone, address, gstin)
            VALUES (:name, :contact_person, :email, :phone, :address, :gstin)
            RETURNING id
        """),
        body.model_dump(),
    ).mappings().first()
    db.commit()
    return _row(db, row["id"])


@router.get("/{company_id}")
def get_company(
    company_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    return _row(db, company_id)


@router.put("/{company_id}")
def update_company(
    company_id: int,
    body: CompanyIn,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    _row(db, company_id)  # 404 if missing
    db.execute(
        text("""
            UPDATE companies
            SET name = :name, contact_person = :contact_person, email = :email,
                phone = :phone, address = :address, gstin = :gstin
            WHERE id = :id
        """),
        {**body.model_dump(), "id": company_id},
    )
    db.commit()
    return _row(db, company_id)


@router.delete("/{company_id}", status_code=204)
def delete_company(
    company_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    _row(db, company_id)
    db.execute(text("DELETE FROM companies WHERE id = :id"), {"id": company_id})
    db.commit()
