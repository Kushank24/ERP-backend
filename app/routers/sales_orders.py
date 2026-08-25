from __future__ import annotations

import json
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/sales-orders", tags=["sales-orders"])


class SalesLineIn(BaseModel):
    product_name: str = Field(min_length=1)
    product_code: Optional[str] = None
    quantity_sold: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    notes: Optional[str] = None
    finished_good_id: Optional[int] = None


class SalesOrderCreate(BaseModel):
    invoice_number: str = Field(min_length=1)
    company_name: str = Field(min_length=1)
    company_location: str = ""
    company_contact: str = ""
    company_gstin: Optional[str] = None
    sales_date: date
    delivery_date: Optional[date] = None
    gst_rate: float = Field(default=18, ge=0)
    delivery_details: dict = Field(default_factory=dict)
    notes: Optional[str] = None
    lines: List[SalesLineIn] = Field(min_length=1)


def _serialize_so(db: Session, so_id: int) -> dict:
    head = db.execute(
        text(
            """
            SELECT id, invoice_number, company_name, company_location, company_contact, company_gstin,
                   sales_date, delivery_date, actual_delivery_date, total_amount, status, gst_rate,
                   delivery_details, notes, created_at, updated_at, payment_received
            FROM sales_orders WHERE id = :id
            """
        ),
        {"id": so_id},
    ).mappings().first()
    if not head:
        raise HTTPException(404, "Sales order not found")
    lines = db.execute(
        text(
            """
            SELECT id, finished_good_id, product_name, product_code, quantity_sold, unit_price, total_price, notes, dispatched_qty
            FROM sales_order_items WHERE sales_order_id = :id
            """
        ),
        {"id": so_id},
    ).mappings().all()
    d = dict(head)
    if isinstance(d.get("delivery_details"), str):
        d["delivery_details"] = json.loads(d["delivery_details"])
    d["lines"] = [dict(x) for x in lines]
    subtotal = sum(float(x["total_price"]) for x in d["lines"])
    d["subtotal"] = subtotal
    d["gst_amount"] = subtotal * (float(d["gst_rate"]) / 100.0)
    return d


@router.get("")
def list_so(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    rows = db.execute(
        text(
            """
            SELECT id, invoice_number, company_name, total_amount, status, sales_date, created_at, payment_received
            FROM sales_orders ORDER BY created_at DESC NULLS LAST
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/companies/list")
def list_companies(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (company_name) company_name, company_location, company_contact, company_gstin
            FROM sales_orders
            WHERE company_name IS NOT NULL AND company_name != ''
            ORDER BY company_name, created_at DESC
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{so_id}")
def get_so(so_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    return _serialize_so(db, so_id)


@router.post("", status_code=201)
def create_so(
    body: SalesOrderCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    dup = db.execute(
        text("SELECT id FROM sales_orders WHERE invoice_number = :n"),
        {"n": body.invoice_number.strip()},
    ).first()
    if dup:
        raise HTTPException(400, "Invoice number already exists")

    line_totals: list[tuple[float, dict]] = []
    for line in body.lines:
        tp = float(line.quantity_sold) * float(line.unit_price)
        line_totals.append((tp, line.model_dump()))

    subtotal = sum(t for t, _ in line_totals)
    gst_amt = subtotal * (body.gst_rate / 100.0)
    total = subtotal + gst_amt

    row = db.execute(
        text(
            """
            INSERT INTO sales_orders (
              invoice_number, company_name, company_location, company_contact, company_gstin,
              sales_date, delivery_date, total_amount, status, gst_rate, delivery_details, notes
            )
            VALUES (
              :inv, :cname, :cloc, :ccon, :gstin, :sdate, :ddate, :total, 1, :grate,
              CAST(:details AS jsonb), :notes
            )
            RETURNING id
            """
        ),
        {
            "inv": body.invoice_number.strip(),
            "cname": body.company_name.strip(),
            "cloc": body.company_location or "",
            "ccon": body.company_contact or "",
            "gstin": body.company_gstin,
            "sdate": body.sales_date,
            "ddate": body.delivery_date,
            "total": total,
            "grate": body.gst_rate,
            "details": json.dumps(body.delivery_details or {}),
            "notes": body.notes,
        },
    ).first()
    so_id = row[0]

    for tp, ld in line_totals:
        db.execute(
            text(
                """
                INSERT INTO sales_order_items (
                  sales_order_id, finished_good_id, product_name, product_code,
                  quantity_sold, unit_price, total_price, notes
                )
                VALUES (:sid, :fgid, :pn, :pc, :qty, :up, :tp, :notes)
                """
            ),
            {
                "sid": so_id,
                "fgid": ld.get("finished_good_id"),
                "pn": ld["product_name"].strip(),
                "pc": ld.get("product_code"),
                "qty": ld["quantity_sold"],
                "up": ld["unit_price"],
                "tp": tp,
                "notes": ld.get("notes"),
            },
        )

    db.commit()
    return _serialize_so(db, so_id)


class PaymentUpdate(BaseModel):
    payment_received: bool

@router.patch("/{so_id}/payment")
def update_payment(so_id: int, body: PaymentUpdate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    row = db.execute(text("SELECT id FROM sales_orders WHERE id = :id"), {"id": so_id}).first()
    if not row:
        raise HTTPException(404, "Sales order not found")
    
    db.execute(
        text("UPDATE sales_orders SET payment_received = :val WHERE id = :id"),
        {"val": body.payment_received, "id": so_id}
    )
    db.commit()
    return _serialize_so(db, so_id)


class DispatchItem(BaseModel):
    line_id: int
    dispatch_qty: float

class DispatchCreate(BaseModel):
    items: List[DispatchItem]

@router.post("/{so_id}/dispatch")
def dispatch_so(so_id: int, body: DispatchCreate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    po_row = db.execute(
        text("SELECT id, status FROM sales_orders WHERE id = :id FOR UPDATE"),
        {"id": so_id}
    ).mappings().first()
    if not po_row:
        raise HTTPException(404, "Sales order not found")

    lines = {
        r["id"]: r
        for r in db.execute(
            text("SELECT id, finished_good_id, product_name, quantity_sold, dispatched_qty FROM sales_order_items WHERE sales_order_id = :id"),
            {"id": so_id}
        ).mappings().all()
    }

    for item in body.items:
        if item.dispatch_qty <= 0:
            continue
        line = lines.get(item.line_id)
        if not line:
            raise HTTPException(400, f"Line {item.line_id} invalid")
        
        needed = line["quantity_sold"] - line["dispatched_qty"]
        if item.dispatch_qty > needed:
            raise HTTPException(400, f"Cannot dispatch {item.dispatch_qty} for {line['product_name']}, max {needed}")

        db.execute(
            text("UPDATE sales_order_items SET dispatched_qty = dispatched_qty + :qty WHERE id = :lid"),
            {"qty": item.dispatch_qty, "lid": item.line_id}
        )

        # Deduct from Finished Goods
        qty_needed = float(item.dispatch_qty)
        if line["finished_good_id"]:
            db.execute(
                text("UPDATE finished_goods SET quantity_in_stock = quantity_in_stock - :qty WHERE id = :id"),
                {"qty": qty_needed, "id": line["finished_good_id"]}
            )
        else:
            pname = line["product_name"].strip()
            # FIFO deduction
            fg_rows = db.execute(
                text(
                    "SELECT id, quantity_in_stock FROM finished_goods WHERE product_name = :pname AND quantity_in_stock > 0 ORDER BY completion_date ASC FOR UPDATE"
                ),
                {"pname": pname}
            ).mappings().all()
            
            rem = qty_needed
            for fg in fg_rows:
                if rem <= 0:
                    break
                deduct = min(rem, float(fg["quantity_in_stock"]))
                db.execute(
                    text("UPDATE finished_goods SET quantity_in_stock = quantity_in_stock - :d WHERE id = :id"),
                    {"d": deduct, "id": fg["id"]}
                )
                rem -= deduct

    # Update SO status
    lines_after = db.execute(
        text("SELECT quantity_sold, dispatched_qty FROM sales_order_items WHERE sales_order_id = :id"),
        {"id": so_id}
    ).mappings().all()
    
    all_done = all(r["dispatched_qty"] >= r["quantity_sold"] for r in lines_after)
    any_done = any(r["dispatched_qty"] > 0 for r in lines_after)

    new_status = po_row["status"]
    if all_done:
        new_status = 4 # Full Dispatch
    elif any_done:
        new_status = 3 # Partial Dispatch

    if new_status != po_row["status"]:
        db.execute(text("UPDATE sales_orders SET status = :st WHERE id = :id"), {"st": new_status, "id": so_id})

    db.commit()
    return _serialize_so(db, so_id)

