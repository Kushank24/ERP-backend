from __future__ import annotations

import io
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..pdf_service import PDFGenerationService

router = APIRouter(prefix="/offers", tags=["offers"])

VALID_STATUSES = {"draft", "sent", "accepted", "rejected", "expired"}
_pdf_svc = PDFGenerationService()


class SpecValueIn(BaseModel):
    specification_id: int
    value: str


class OfferItemIn(BaseModel):
    product_id: Optional[int] = None
    description: str = Field(min_length=1)
    quantity: int = Field(default=1, ge=1)
    unit_price: float = Field(default=0, ge=0)
    specifications: List[SpecValueIn] = Field(default_factory=list)


class OfferCreate(BaseModel):
    enquiry_id: Optional[int] = None
    company_id: Optional[int] = None
    offer_number: str = Field(min_length=1)
    offer_date: date = Field(default_factory=date.today)
    valid_until: Optional[date] = None
    currency: str = "INR"
    packing_charges_pct: float = 0
    freight_charges: float = 0
    gst_pct: float = 18
    kind_attn: Optional[str] = None
    terms_conditions: Optional[str] = None
    notes: Optional[str] = None
    items: List[OfferItemIn] = Field(default_factory=list)


class OfferUpdate(OfferCreate):
    pass


class StatusUpdate(BaseModel):
    status: str
    follow_up_comments: Optional[str] = None


def _calc_totals(items: list, packing_pct: float, freight: float, gst_pct: float) -> dict:
    subtotal = sum(i.quantity * i.unit_price for i in items)
    packing = subtotal * (packing_pct / 100)
    assessable = subtotal + packing + freight
    gst_amt = assessable * (gst_pct / 100)
    total = assessable + gst_amt
    return {"subtotal": subtotal, "total_amount": total}


def _get_status(db: Session, offer_id: int) -> str:
    row = db.execute(
        text("SELECT status FROM offers WHERE id = :id"), {"id": offer_id}
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Offer not found")
    return row[0]


def _serialize(db: Session, offer_id: int) -> dict:
    offer = db.execute(
        text("""
            SELECT o.*,
                   c.name AS company_name, c.gstin AS company_gstin,
                   c.address AS company_address,
                   c.contact_person AS company_contact_person,
                   COALESCE(o.kind_attn, c.contact_person) AS contact_person,
                   c.phone AS company_phone, c.email AS company_email,
                   e.enquiry_number,
                   e.reference_number AS enquiry_reference_number
            FROM offers o
            LEFT JOIN companies c ON c.id = o.company_id
            LEFT JOIN enquiries e ON e.id = o.enquiry_id
            WHERE o.id = :id
        """),
        {"id": offer_id},
    ).mappings().first()
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")

    items = db.execute(
        text("""
            SELECT oi.*, cp.model_name AS product_name_resolved
            FROM offer_items oi
            LEFT JOIN catalog_products cp ON cp.id = oi.product_id
            WHERE oi.offer_id = :id
            ORDER BY oi.id
        """),
        {"id": offer_id},
    ).mappings().all()

    # Fetch ALL specs for ALL items in one query instead of one query per item
    item_ids = [item["id"] for item in items]
    specs_by_item: dict[int, list] = {iid: [] for iid in item_ids}
    if item_ids:
        all_specs = db.execute(
            text("""
                SELECT ois.offer_item_id, ois.specification_id, ois.value, s.name AS spec_name
                FROM offer_item_specifications ois
                JOIN specifications s ON s.id = ois.specification_id
                WHERE ois.offer_item_id = ANY(:ids)
                ORDER BY ois.offer_item_id, ois.specification_id
            """),
            {"ids": item_ids},
        ).mappings().all()
        for row in all_specs:
            specs_by_item[row["offer_item_id"]].append(dict(row))

    item_list = [
        {**dict(item), "specifications": specs_by_item[item["id"]]}
        for item in items
    ]

    return {**dict(offer), "items": item_list}


@router.get("/price-history")
def get_price_history(
    company_id: int,
    product_id: int,
    limit: int = 5,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    """Return the last N unit prices quoted to a company for a specific product."""
    rows = db.execute(
        text("""
            SELECT o.offer_number, o.offer_date, o.status,
                   oi.unit_price, oi.quantity
            FROM offer_items oi
            JOIN offers o ON o.id = oi.offer_id
            WHERE o.company_id = :company_id AND oi.product_id = :product_id
            ORDER BY o.offer_date DESC, o.id DESC
            LIMIT :limit
        """),
        {"company_id": company_id, "product_id": product_id, "limit": limit},
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("")
def list_offers(
    status: Optional[str] = None,
    company_id: Optional[int] = None,
    q: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    where = []
    params: dict = {}
    if status:
        where.append("o.status = :status")
        params["status"] = status
    if company_id:
        where.append("o.company_id = :company_id")
        params["company_id"] = company_id
    if q:
        where.append("(c.name ILIKE :q OR o.offer_number ILIKE :q)")
        params["q"] = f"%{q}%"
    if date_from:
        where.append("o.created_at::date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where.append("o.created_at::date <= :date_to")
        params["date_to"] = date_to

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    base = f"""
        FROM offers o
        LEFT JOIN companies c ON c.id = o.company_id
        LEFT JOIN enquiries e ON e.id = o.enquiry_id
        {clause}
    """
    total = db.execute(text(f"SELECT COUNT(*) {base}"), params).scalar()
    params["limit"] = limit
    params["offset"] = offset
    rows = db.execute(
        text(f"""
            SELECT o.*, c.name AS company_name, e.enquiry_number {base}
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    ).mappings().all()
    return {"data": [dict(r) for r in rows], "total": total}


@router.post("", status_code=201)
def create_offer(
    body: OfferCreate,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    # Check offer_number uniqueness
    existing = db.execute(
        text("SELECT id FROM offers WHERE offer_number = :n"), {"n": body.offer_number}
    ).first()
    if existing:
        raise HTTPException(400, "Offer number already exists")

    totals = _calc_totals(body.items, body.packing_charges_pct, body.freight_charges, body.gst_pct)

    row = db.execute(
        text("""
            INSERT INTO offers
                (enquiry_id, company_id, offer_number, offer_date, valid_until,
                 currency, packing_charges_pct, freight_charges, gst_pct,
                 kind_attn, subtotal, total_amount, terms_conditions, notes)
            VALUES
                (:enquiry_id, :company_id, :offer_number, :offer_date, :valid_until,
                 :currency, :packing_charges_pct, :freight_charges, :gst_pct,
                 :kind_attn, :subtotal, :total_amount, :terms_conditions, :notes)
            RETURNING id
        """),
        {
            "enquiry_id": body.enquiry_id,
            "company_id": body.company_id,
            "offer_number": body.offer_number,
            "offer_date": body.offer_date,
            "valid_until": body.valid_until,
            "currency": body.currency,
            "packing_charges_pct": body.packing_charges_pct,
            "freight_charges": body.freight_charges,
            "gst_pct": body.gst_pct,
            "kind_attn": body.kind_attn or None,
            "subtotal": totals["subtotal"],
            "total_amount": totals["total_amount"],
            "terms_conditions": body.terms_conditions,
            "notes": body.notes,
        },
    ).mappings().first()
    offer_id = row["id"]

    _insert_items(db, offer_id, body.items)

    # If linked to an enquiry, mark it as offer_sent
    if body.enquiry_id:
        db.execute(
            text("UPDATE enquiries SET status = 'offer_sent' WHERE id = :id"),
            {"id": body.enquiry_id},
        )

    db.commit()
    return _serialize(db, offer_id)


@router.get("/{offer_id}")
def get_offer(
    offer_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    return _serialize(db, offer_id)


@router.put("/{offer_id}")
def update_offer(
    offer_id: int,
    body: OfferUpdate,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    if _get_status(db, offer_id) == "accepted":
        raise HTTPException(400, "Accepted offers cannot be edited")

    totals = _calc_totals(body.items, body.packing_charges_pct, body.freight_charges, body.gst_pct)

    db.execute(
        text("""
            UPDATE offers
            SET enquiry_id = :enquiry_id, company_id = :company_id,
                offer_number = :offer_number, offer_date = :offer_date,
                valid_until = :valid_until, currency = :currency,
                packing_charges_pct = :packing_charges_pct, freight_charges = :freight_charges,
                gst_pct = :gst_pct, kind_attn = :kind_attn,
                subtotal = :subtotal, total_amount = :total_amount,
                terms_conditions = :terms_conditions, notes = :notes
            WHERE id = :id
        """),
        {
            "id": offer_id,
            "enquiry_id": body.enquiry_id,
            "company_id": body.company_id,
            "offer_number": body.offer_number,
            "offer_date": body.offer_date,
            "valid_until": body.valid_until,
            "currency": body.currency,
            "packing_charges_pct": body.packing_charges_pct,
            "freight_charges": body.freight_charges,
            "gst_pct": body.gst_pct,
            "kind_attn": body.kind_attn or None,
            "subtotal": totals["subtotal"],
            "total_amount": totals["total_amount"],
            "terms_conditions": body.terms_conditions,
            "notes": body.notes,
        },
    )

    db.execute(text("DELETE FROM offer_items WHERE offer_id = :id"), {"id": offer_id})
    _insert_items(db, offer_id, body.items)
    db.commit()
    return _serialize(db, offer_id)


@router.patch("/{offer_id}/status")
def update_status(
    offer_id: int,
    body: StatusUpdate,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Choose from: {VALID_STATUSES}")

    if _get_status(db, offer_id) == "accepted":
        raise HTTPException(400, "Accepted offers cannot be changed")

    db.execute(
        text("""
            UPDATE offers
            SET status = :status, follow_up_comments = :comments
            WHERE id = :id
        """),
        {"id": offer_id, "status": body.status, "comments": body.follow_up_comments},
    )

    # Auto-create Sales Order when an offer is accepted
    if body.status == "accepted":
        offer = db.execute(
            text("""
                SELECT o.*, c.name AS company_name, c.gstin AS company_gstin
                FROM offers o
                LEFT JOIN companies c ON c.id = o.company_id
                WHERE o.id = :id
            """),
            {"id": offer_id},
        ).mappings().first()

    if body.status == "accepted" and offer and not offer.get("sales_order_id"):
        so_row = db.execute(
            text("""
                INSERT INTO sales_orders
                    (invoice_number, company_name, company_gstin, sales_date, gst_rate, status, notes)
                VALUES
                    (:invoice_number, :company_name, :company_gstin, :sales_date, :gst_rate, 'pending', :notes)
                RETURNING id
            """),
            {
                "invoice_number": f"SO-{offer['offer_number']}",
                "company_name": offer.get("company_name") or "Unknown",
                "company_gstin": offer.get("company_gstin"),
                "sales_date": offer["offer_date"],
                "gst_rate": float(offer["gst_pct"]),
                "notes": f"Auto-created from offer {offer['offer_number']}",
            },
        ).mappings().first()
        so_id = so_row["id"]

        # Copy offer items → sales_order_lines
        items = db.execute(
            text("SELECT * FROM offer_items WHERE offer_id = :id ORDER BY id"),
            {"id": offer_id},
        ).mappings().all()
        for item in items:
            db.execute(
                text("""
                    INSERT INTO sales_order_lines
                        (sales_order_id, product_name, quantity_sold, unit_price, notes)
                    VALUES
                        (:so_id, :product_name, :qty, :unit_price, NULL)
                """),
                {
                    "so_id": so_id,
                    "product_name": item["description"],
                    "qty": item["quantity"],
                    "unit_price": float(item["unit_price"]),
                },
            )

        db.execute(
            text("UPDATE offers SET sales_order_id = :so_id WHERE id = :id"),
            {"so_id": so_id, "id": offer_id},
        )

        # Mark linked enquiry as completed
        if offer.get("enquiry_id"):
            db.execute(
                text("UPDATE enquiries SET status = 'completed' WHERE id = :id"),
                {"id": offer["enquiry_id"]},
            )

    db.commit()
    return _serialize(db, offer_id)


@router.delete("/{offer_id}", status_code=204)
def delete_offer(
    offer_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    if _get_status(db, offer_id) == "accepted":
        raise HTTPException(400, "Accepted offers cannot be deleted")
    db.execute(text("DELETE FROM offers WHERE id = :id"), {"id": offer_id})
    db.commit()


@router.get("/{offer_id}/pdf")
def download_offer_pdf(
    offer_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(get_current_user),
):
    offer = _serialize(db, offer_id)
    pdf_bytes = _pdf_svc.generate_offer_pdf(offer)
    safe_num = offer["offer_number"].replace("/", "-")
    return StreamingResponse(
        io.BytesIO(pdf_bytes.getvalue()),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Offer-{safe_num}.pdf"'},
    )


# ── helpers ─────────────────────────────────────────────────────────────────

def _insert_items(db: Session, offer_id: int, items: list) -> None:
    if not items:
        return

    # Insert ALL items in one batched query using UNNEST — 1 round-trip instead of N
    # Use CAST(... AS type[]) instead of ::type[] to avoid SQLAlchemy text() parser
    # choking on the double-colon token.
    item_rows = db.execute(
        text("""
            INSERT INTO offer_items
                (offer_id, product_id, description, quantity, unit_price, total_price)
            SELECT
                :offer_id,
                unnest(CAST(:product_ids AS int[])),
                unnest(CAST(:descriptions AS text[])),
                unnest(CAST(:quantities AS int[])),
                unnest(CAST(:unit_prices AS numeric[])),
                unnest(CAST(:total_prices AS numeric[]))
            RETURNING id
        """),
        {
            "offer_id": offer_id,
            "product_ids": [item.product_id for item in items],
            "descriptions": [item.description for item in items],
            "quantities": [item.quantity for item in items],
            "unit_prices": [item.unit_price for item in items],
            "total_prices": [item.quantity * item.unit_price for item in items],
        },
    ).all()

    # Collect all specs across all items, then insert in one batched query
    spec_item_ids: list[int] = []
    spec_spec_ids: list[int] = []
    spec_values: list[str] = []
    for item, row in zip(items, item_rows):
        for spec in (item.specifications or []):
            if spec.value.strip():
                spec_item_ids.append(row[0])
                spec_spec_ids.append(spec.specification_id)
                spec_values.append(spec.value.strip())

    if spec_item_ids:
        db.execute(
            text("""
                INSERT INTO offer_item_specifications (offer_item_id, specification_id, value)
                SELECT
                    unnest(CAST(:item_ids AS int[])),
                    unnest(CAST(:spec_ids AS int[])),
                    unnest(CAST(:vals AS text[]))
                ON CONFLICT (offer_item_id, specification_id) DO UPDATE SET value = EXCLUDED.value
            """),
            {"item_ids": spec_item_ids, "spec_ids": spec_spec_ids, "vals": spec_values},
        )
