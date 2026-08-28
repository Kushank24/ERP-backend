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


class SalesLineUpdate(SalesLineIn):
    id: Optional[int] = None  # None = new line; set = existing line


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


class SalesOrderUpdate(BaseModel):
    company_name: str = Field(min_length=1)
    company_location: str = ""
    company_contact: str = ""
    company_gstin: Optional[str] = None
    sales_date: date
    delivery_date: Optional[date] = None
    gst_rate: float = Field(default=18, ge=0)
    delivery_details: dict = Field(default_factory=dict)
    notes: Optional[str] = None
    lines: List[SalesLineUpdate] = Field(min_length=1)


def _serialize_so(db: Session, so_id: int) -> dict:
    head = db.execute(
        text(
            """
            SELECT id, invoice_number, company_name, company_location, company_contact, company_gstin,
                   sales_date, delivery_date, actual_delivery_date, total_amount, status, gst_rate,
                   delivery_details, notes, created_at, updated_at, payment_received, payment_amount,
                   COALESCE(additional_costs, '[]'::jsonb) AS additional_costs
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
    if isinstance(d.get("additional_costs"), str):
        d["additional_costs"] = json.loads(d["additional_costs"])
    if d.get("additional_costs") is None:
        d["additional_costs"] = []
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
            SELECT id, invoice_number, company_name, total_amount, status, sales_date, created_at, payment_received, payment_amount
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
        qty_needed = float(ld["quantity_sold"])
        fgid = ld.get("finished_good_id")

        if fgid:
            fg = db.execute(
                text("SELECT id, product_name, quantity_in_stock FROM finished_goods WHERE id = :id FOR UPDATE"),
                {"id": fgid},
            ).mappings().first()
            if not fg:
                raise HTTPException(400, f"Finished good id {fgid} not found")
            if float(fg["quantity_in_stock"]) < qty_needed:
                raise HTTPException(
                    400,
                    f"Insufficient stock for '{fg['product_name']}': available {float(fg['quantity_in_stock'])}, required {qty_needed}",
                )
            db.execute(
                text("UPDATE finished_goods SET quantity_in_stock = quantity_in_stock - :qty WHERE id = :id"),
                {"qty": qty_needed, "id": fgid},
            )
        else:
            pname = ld["product_name"].strip()
            fg_rows = db.execute(
                text(
                    "SELECT id, quantity_in_stock FROM finished_goods "
                    "WHERE product_name = :pname AND quantity_in_stock > 0 "
                    "ORDER BY completion_date ASC FOR UPDATE"
                ),
                {"pname": pname},
            ).mappings().all()
            available = sum(float(r["quantity_in_stock"]) for r in fg_rows)
            if available < qty_needed:
                raise HTTPException(
                    400,
                    f"Insufficient stock for '{pname}': available {available}, required {qty_needed}",
                )
            rem = qty_needed
            for fg in fg_rows:
                if rem <= 0:
                    break
                deduct = min(rem, float(fg["quantity_in_stock"]))
                db.execute(
                    text("UPDATE finished_goods SET quantity_in_stock = quantity_in_stock - :d WHERE id = :id"),
                    {"d": deduct, "id": fg["id"]},
                )
                rem -= deduct

        db.execute(
            text(
                """
                INSERT INTO sales_order_items (
                  sales_order_id, finished_good_id, product_name, product_code,
                  quantity_sold, unit_price, total_price, notes, dispatched_qty
                )
                VALUES (:sid, :fgid, :pn, :pc, :qty, :up, :tp, :notes, :qty)
                """
            ),
            {
                "sid": so_id,
                "fgid": fgid,
                "pn": ld["product_name"].strip(),
                "pc": ld.get("product_code"),
                "qty": qty_needed,
                "up": ld["unit_price"],
                "tp": tp,
                "notes": ld.get("notes"),
            },
        )

    db.commit()
    return _serialize_so(db, so_id)


@router.patch("/{so_id}")
def update_so(
    so_id: int,
    body: SalesOrderUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    if not db.execute(text("SELECT id FROM sales_orders WHERE id = :id"), {"id": so_id}).first():
        raise HTTPException(404, "Sales order not found")

    current_lines = {
        r["id"]: dict(r)
        for r in db.execute(
            text("SELECT id, dispatched_qty FROM sales_order_items WHERE sales_order_id = :id"),
            {"id": so_id},
        ).mappings().all()
    }
    incoming_ids = {l.id for l in body.lines if l.id is not None}

    # Block removal of lines that have been partially/fully dispatched
    for lid, line in current_lines.items():
        if lid not in incoming_ids and float(line["dispatched_qty"] or 0) > 0:
            raise HTTPException(
                400,
                f"Cannot remove a line that has already been dispatched (line id {lid}).",
            )

    # Delete safe-to-remove lines
    for lid in current_lines:
        if lid not in incoming_ids:
            db.execute(text("DELETE FROM sales_order_items WHERE id = :id"), {"id": lid})

    new_subtotal = 0.0
    for line in body.lines:
        tp = float(line.quantity_sold) * float(line.unit_price)
        new_subtotal += tp
        if line.id and line.id in current_lines:
            dispatched = float(current_lines[line.id]["dispatched_qty"] or 0)
            if line.quantity_sold < dispatched:
                raise HTTPException(
                    400,
                    f"Quantity for '{line.product_name}' cannot be less than already dispatched ({dispatched}).",
                )
            db.execute(
                text(
                    "UPDATE sales_order_items SET product_name=:pn, product_code=:pc, "
                    "quantity_sold=:qty, unit_price=:up, total_price=:tp, notes=:notes "
                    "WHERE id=:lid"
                ),
                {"pn": line.product_name, "pc": line.product_code, "qty": line.quantity_sold,
                 "up": line.unit_price, "tp": tp, "notes": line.notes, "lid": line.id},
            )
        else:
            db.execute(
                text(
                    "INSERT INTO sales_order_items "
                    "(sales_order_id, finished_good_id, product_name, product_code, quantity_sold, unit_price, total_price, notes) "
                    "VALUES (:sid, :fgid, :pn, :pc, :qty, :up, :tp, :notes)"
                ),
                {"sid": so_id, "fgid": line.finished_good_id, "pn": line.product_name,
                 "pc": line.product_code, "qty": line.quantity_sold, "up": line.unit_price,
                 "tp": tp, "notes": line.notes},
            )

    gst_amt = new_subtotal * (body.gst_rate / 100.0)
    new_total = new_subtotal + gst_amt

    db.execute(
        text(
            "UPDATE sales_orders SET company_name=:cname, company_location=:cloc, company_contact=:ccon, "
            "company_gstin=:gstin, sales_date=:sdate, delivery_date=:ddate, gst_rate=:grate, "
            "delivery_details=CAST(:details AS jsonb), notes=:notes, total_amount=:total, updated_at=now() "
            "WHERE id=:id"
        ),
        {
            "cname": body.company_name.strip(), "cloc": body.company_location, "ccon": body.company_contact,
            "gstin": body.company_gstin, "sdate": body.sales_date, "ddate": body.delivery_date,
            "grate": body.gst_rate, "details": json.dumps(body.delivery_details or {}),
            "notes": body.notes, "total": new_total, "id": so_id,
        },
    )
    db.commit()
    return _serialize_so(db, so_id)


class PaymentUpdate(BaseModel):
    # 1 = Not Received, 2 = Partially Received, 3 = Received
    payment_status: int = Field(ge=1, le=3)
    payment_amount: Optional[float] = Field(default=None, ge=0)

@router.patch("/{so_id}/payment")
def update_payment(so_id: int, body: PaymentUpdate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    row = db.execute(text("SELECT id, total_amount FROM sales_orders WHERE id = :id"), {"id": so_id}).first()
    if not row:
        raise HTTPException(404, "Sales order not found")
    if body.payment_status == 2 and body.payment_amount is not None:
        if body.payment_amount > float(row.total_amount):
            raise HTTPException(400, f"Payment amount cannot exceed the order total of ₹{float(row.total_amount):.2f}.")
    # Clear amount when moving away from Partial
    amt = body.payment_amount if body.payment_status == 2 else None
    db.execute(
        text("UPDATE sales_orders SET status = :st, payment_amount = :amt, updated_at = now() WHERE id = :id"),
        {"st": body.payment_status, "amt": amt, "id": so_id}
    )
    db.commit()
    return _serialize_so(db, so_id)


class AdditionalCostItem(BaseModel):
    label: str = Field(min_length=1)
    amount: float = Field(ge=0)

class AdditionalCostsBody(BaseModel):
    items: List[AdditionalCostItem] = Field(default_factory=list)

@router.patch("/{so_id}/additional-costs")
def update_additional_costs(so_id: int, body: AdditionalCostsBody, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    row = db.execute(
        text("SELECT gst_rate FROM sales_orders WHERE id = :id"), {"id": so_id}
    ).first()
    if not row:
        raise HTTPException(404, "Sales order not found")
    subtotal = db.execute(
        text("SELECT COALESCE(SUM(total_price), 0) FROM sales_order_items WHERE sales_order_id = :id"),
        {"id": so_id},
    ).scalar()
    subtotal = float(subtotal or 0)
    extra = sum(float(i.amount) for i in body.items)
    gst_base = subtotal + extra
    gst_amt = gst_base * (float(row.gst_rate) / 100.0)
    new_total = gst_base + gst_amt
    items_json = json.dumps([{"label": i.label, "amount": i.amount} for i in body.items])
    db.execute(
        text("UPDATE sales_orders SET additional_costs = CAST(:ac AS jsonb), total_amount = :total, updated_at = now() WHERE id = :id"),
        {"ac": items_json, "total": new_total, "id": so_id},
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

