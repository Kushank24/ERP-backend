from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/catalog-products", tags=["catalog-products"])


class CatalogProductCreate(BaseModel):
    model_name: str = Field(min_length=1)
    code: Optional[str] = None
    category: Optional[str] = None
    definition: Optional[str] = None


class CatalogProductPatch(BaseModel):
    model_name: Optional[str] = Field(default=None, min_length=1)
    code: Optional[str] = None
    category: Optional[str] = None
    definition: Optional[str] = None


class SpecsUpdate(BaseModel):
    specification_ids: List[int] = Field(default_factory=list)


def _row(db: Session, cp_id: int) -> dict:
    r = db.execute(
        text("SELECT id, model_name, code, category, definition, created_at FROM catalog_products WHERE id = :id"),
        {"id": cp_id},
    ).mappings().first()
    if not r:
        raise HTTPException(404, "Catalog product not found")
    return dict(r)


@router.get("/categories")
def list_categories(
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    rows = db.execute(
        text("SELECT DISTINCT category FROM catalog_products WHERE category IS NOT NULL ORDER BY category")
    ).scalars().all()
    return rows


@router.get("")
def list_catalog_products(
    search: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    has_specs: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    conditions: list[str] = []
    params: dict = {}

    if search:
        conditions.append(
            "(cp.model_name ILIKE :search OR cp.code ILIKE :search OR cp.category ILIKE :search)"
        )
        params["search"] = f"%{search.strip()}%"

    if category is not None:
        if category == "":
            conditions.append("(cp.category IS NULL OR cp.category = '')")
        else:
            conditions.append("cp.category = :category")
            params["category"] = category

    if has_specs:
        conditions.append(
            "EXISTS (SELECT 1 FROM catalog_product_specifications cps WHERE cps.catalog_product_id = cp.id)"
        )

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total = db.execute(
        text(f"SELECT COUNT(*) FROM catalog_products cp {where}"), params
    ).scalar_one()

    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    rows = db.execute(
        text(f"""
            SELECT cp.id, cp.model_name, cp.code, cp.category, cp.definition, cp.created_at
            FROM catalog_products cp
            {where}
            ORDER BY cp.model_name
            LIMIT :limit OFFSET :offset
        """),
        params,
    ).mappings().all()

    return {"items": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/specifications/catalog")
def list_all_specifications(
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    rows = db.execute(
        text("""
            SELECT s.id, s.name, COUNT(cps.catalog_product_id)::int AS product_count
            FROM specifications s
            LEFT JOIN catalog_product_specifications cps ON cps.specification_id = s.id
            GROUP BY s.id, s.name
            ORDER BY s.name
        """)
    ).mappings().all()
    return [dict(r) for r in rows]


class SpecCreate(BaseModel):
    name: str = Field(min_length=1)


@router.post("/specifications/catalog", status_code=201)
def create_specification(
    body: SpecCreate,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    existing = db.execute(
        text("SELECT id, name FROM specifications WHERE name = :name"),
        {"name": body.name.strip()},
    ).mappings().first()
    if existing:
        return dict(existing)
    row = db.execute(
        text("INSERT INTO specifications (name) VALUES (:name) RETURNING id, name"),
        {"name": body.name.strip()},
    ).mappings().first()
    db.commit()
    return dict(row)


@router.get("/{cp_id}/specifications")
def get_specifications(
    cp_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    rows = db.execute(
        text("""
            SELECT cps.id, cps.specification_id, s.name AS spec_name, cps.display_order
            FROM catalog_product_specifications cps
            JOIN specifications s ON s.id = cps.specification_id
            WHERE cps.catalog_product_id = :id
            ORDER BY cps.display_order ASC, s.name ASC
        """),
        {"id": cp_id},
    ).mappings().all()
    return [dict(r) for r in rows]


@router.put("/{cp_id}/specifications")
def set_specifications(
    cp_id: int,
    body: SpecsUpdate,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    if not db.execute(text("SELECT 1 FROM catalog_products WHERE id = :id"), {"id": cp_id}).first():
        raise HTTPException(404, "Catalog product not found")
    db.execute(
        text("DELETE FROM catalog_product_specifications WHERE catalog_product_id = :id"),
        {"id": cp_id},
    )
    deduped = list(dict.fromkeys(body.specification_ids))
    if deduped:
        db.execute(
            text("""
                INSERT INTO catalog_product_specifications (catalog_product_id, specification_id, display_order)
                SELECT :cp_id,
                    unnest(CAST(:spec_ids AS int[])),
                    unnest(CAST(:orders AS int[]))
            """),
            {
                "cp_id": cp_id,
                "spec_ids": deduped,
                "orders": list(range(len(deduped))),
            },
        )
    db.commit()
    return get_specifications(cp_id, db, _user)


@router.get("/{cp_id}")
def get_catalog_product(
    cp_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    return _row(db, cp_id)


@router.post("", status_code=201)
def create_catalog_product(
    body: CatalogProductCreate,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    row = db.execute(
        text("""
            INSERT INTO catalog_products (model_name, code, category, definition)
            VALUES (:model_name, :code, :category, :definition)
            RETURNING id
        """),
        {"model_name": body.model_name.strip(), "code": body.code, "category": body.category, "definition": body.definition},
    ).mappings().first()
    db.commit()
    return _row(db, row["id"])


@router.patch("/{cp_id}")
def patch_catalog_product(
    cp_id: int,
    body: CatalogProductPatch,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    _row(db, cp_id)
    fields = body.model_dump(exclude_unset=True)
    if fields:
        sets = [f"{k} = :{k}" for k in fields]
        db.execute(
            text(f"UPDATE catalog_products SET {', '.join(sets)} WHERE id = :id"),
            {"id": cp_id, **fields},
        )
        db.commit()
    return _row(db, cp_id)


@router.delete("/{cp_id}", status_code=204)
def delete_catalog_product(
    cp_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    _row(db, cp_id)
    db.execute(text("DELETE FROM catalog_products WHERE id = :id"), {"id": cp_id})
    db.commit()
