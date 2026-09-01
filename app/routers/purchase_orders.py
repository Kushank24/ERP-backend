from __future__ import annotations

import json
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..pdf_service import PDFGenerationService

_pdf_svc = PDFGenerationService()

router = APIRouter(prefix="/purchase-orders", tags=["purchase-orders"])


class PoLineIn(BaseModel):
    material_name: str = Field(min_length=1)
    length_weight_nos: float = Field(gt=0)
    per_unit_cost: float = Field(ge=0)
    unit: Optional[str] = None
    comment: Optional[str] = None


class PoLineUpdate(PoLineIn):
    id: Optional[int] = None  # None = new line; set = existing line


class PurchaseOrderCreate(BaseModel):
    purchase_number: str = Field(min_length=1)
    supplier_name: str = Field(min_length=1)
    supplier_location: str = ""
    supplier_contact: str = ""
    supplier_gstin: Optional[str] = None
    order_delivery_date: Optional[date] = None
    gst_rate: float = Field(default=18, ge=0)
    delivery_details: dict = Field(default_factory=dict)
    remarks: Optional[str] = None
    lines: List[PoLineIn] = Field(min_length=1)


class PurchaseOrderUpdate(BaseModel):
    supplier_name: str = Field(min_length=1)
    supplier_location: str = ""
    supplier_contact: str = ""
    supplier_gstin: Optional[str] = None
    order_delivery_date: Optional[date] = None
    gst_rate: float = Field(default=18, ge=0)
    delivery_details: dict = Field(default_factory=dict)
    remarks: Optional[str] = None
    lines: List[PoLineUpdate] = Field(min_length=1)


def _supplier_id(
    db: Session,
    supplier_name: str,
    supplier_location: str = "",
    supplier_contact: str = "",
    supplier_gstin: Optional[str] = None,
) -> int:
    row = db.execute(
        text("SELECT id FROM suppliers WHERE name = :n"),
        {"n": supplier_name.strip()},
    ).first()
    if row:
        return row[0]
    ins = db.execute(
        text(
            """
            INSERT INTO suppliers (name, location, contact, gstin)
            VALUES (:name, :loc, :contact, :gstin)
            RETURNING id
            """
        ),
        {
            "name": supplier_name.strip(),
            "loc": supplier_location or "",
            "contact": supplier_contact or "",
            "gstin": supplier_gstin,
        },
    ).first()
    return ins[0]


def _serialize_po(db: Session, po_id: int) -> dict:
    head = db.execute(
        text(
            """
            SELECT p.id, p.purchase_number, p.purchase_date, p.total_amount, p.order_delivery_date,
                   p.status, p.gst_rate, p.actual_delivery_date, p.bill_numbers, p.delivery_details,
                   p.remarks, COALESCE(p.additional_costs, '[]'::jsonb) AS additional_costs,
                   s.name AS supplier_name, s.location AS supplier_location, s.contact AS supplier_contact,
                   s.gstin AS supplier_gstin
            FROM purchase_orders p
            LEFT JOIN suppliers s ON s.id = p.supplier_id
            WHERE p.id = :id
            """
        ),
        {"id": po_id},
    ).mappings().first()
    if not head:
        raise HTTPException(404, "Purchase order not found")
    lines = db.execute(
        text(
            """
            SELECT id, material_name, length_weight_nos, per_unit_cost, unit, comment, delivered_qty
            FROM purchase_order_lines WHERE purchase_order_id = :id
            """
        ),
        {"id": po_id},
    ).mappings().all()
    d = dict(head)
    if isinstance(d.get("delivery_details"), str):
        d["delivery_details"] = json.loads(d["delivery_details"])
    if isinstance(d.get("additional_costs"), str):
        d["additional_costs"] = json.loads(d["additional_costs"])
    if d.get("additional_costs") is None:
        d["additional_costs"] = []
    d["lines"] = [dict(x) for x in lines]
    return d


@router.get("")
def list_pos(
    q: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    conds = []
    params: dict = {}
    if q and q.strip():
        conds.append("(p.purchase_number ILIKE :q OR COALESCE(s.name,'') ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if date_from:
        conds.append("p.created_at::date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        conds.append("p.created_at::date <= :date_to")
        params["date_to"] = date_to
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = db.execute(
        text(f"""
            SELECT p.id, p.purchase_number, p.total_amount, p.status, p.order_delivery_date, p.created_at,
                   s.name AS supplier_name
            FROM purchase_orders p
            LEFT JOIN suppliers s ON s.id = p.supplier_id
            {where}
            ORDER BY p.created_at DESC NULLS LAST
        """),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{po_id}")
def get_po(po_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    return _serialize_po(db, po_id)

@router.get("/suppliers/list")
def list_suppliers(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    rows = db.execute(
        text("SELECT id, name AS supplier_name, location AS supplier_location, contact AS supplier_contact, gstin AS supplier_gstin FROM suppliers ORDER BY name")
    ).mappings().all()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
def create_po(
    body: PurchaseOrderCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    exists = db.execute(
        text("SELECT id FROM purchase_orders WHERE purchase_number = :n"),
        {"n": body.purchase_number.strip()},
    ).first()
    if exists:
        raise HTTPException(400, "Purchase number already exists")

    total = sum(float(l.length_weight_nos) * float(l.per_unit_cost) for l in body.lines)
    sid = _supplier_id(db, body.supplier_name, body.supplier_location, body.supplier_contact, body.supplier_gstin)

    row = db.execute(
        text(
            """
            INSERT INTO purchase_orders (
              purchase_number, supplier_id, purchase_reference, total_amount,
              order_delivery_date, status, gst_rate, delivery_details, remarks
            )
            VALUES (:num, :sid, '', :total, :deldate, 1, :gst, CAST(:details AS jsonb), :remarks)
            RETURNING id
            """
        ),
        {
            "num": body.purchase_number.strip(),
            "sid": sid,
            "total": total,
            "deldate": body.order_delivery_date,
            "gst": body.gst_rate,
            "details": json.dumps(body.delivery_details or {}),
            "remarks": body.remarks,
        },
    ).first()
    po_id = row[0]

    for line in body.lines:
        line_total = float(line.length_weight_nos) * float(line.per_unit_cost)
        db.execute(
            text(
                """
                INSERT INTO purchase_order_lines (
                  purchase_order_id, material_name, length_weight_nos, per_unit_cost, unit, comment
                )
                VALUES (:pid, :mn, :lwn, :cost, :unit, :comment)
                """
            ),
            {
                "pid": po_id,
                "mn": line.material_name.strip(),
                "lwn": line.length_weight_nos,
                "cost": line.per_unit_cost,
                "unit": line.unit,
                "comment": line.comment,
            },
        )

    db.commit()
    return _serialize_po(db, po_id)

@router.patch("/{po_id}")
def update_po(
    po_id: int,
    body: PurchaseOrderUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    existing = db.execute(
        text("SELECT id FROM purchase_orders WHERE id = :id"), {"id": po_id}
    ).first()
    if not existing:
        raise HTTPException(404, "Purchase order not found")

    # Reconcile lines
    current_lines = {
        r["id"]: dict(r)
        for r in db.execute(
            text("SELECT id, delivered_qty FROM purchase_order_lines WHERE purchase_order_id = :id"),
            {"id": po_id},
        ).mappings().all()
    }
    incoming_ids = {l.id for l in body.lines if l.id is not None}

    # Block removal of lines that have been partially/fully received
    for lid, line in current_lines.items():
        if lid not in incoming_ids and float(line["delivered_qty"] or 0) > 0:
            raise HTTPException(
                400,
                f"Cannot remove a line that has already been received (line id {lid}). "
                "Reduce its quantity to the delivered amount instead.",
            )

    # Update supplier
    sid = _supplier_id(db, body.supplier_name, body.supplier_location, body.supplier_contact, body.supplier_gstin)

    # Delete removed lines (only safe ones — guarded above)
    for lid in current_lines:
        if lid not in incoming_ids:
            db.execute(text("DELETE FROM purchase_order_lines WHERE id = :id"), {"id": lid})

    # Update / insert lines
    new_total = 0.0
    for line in body.lines:
        line_total = float(line.length_weight_nos) * float(line.per_unit_cost)
        new_total += line_total
        if line.id and line.id in current_lines:
            # Validate: new qty must cover already-delivered qty
            delivered = float(current_lines[line.id]["delivered_qty"] or 0)
            if line.length_weight_nos < delivered:
                raise HTTPException(
                    400,
                    f"Quantity for '{line.material_name}' cannot be less than already delivered ({delivered}).",
                )
            db.execute(
                text(
                    "UPDATE purchase_order_lines "
                    "SET material_name=:mn, length_weight_nos=:lwn, per_unit_cost=:cost, unit=:unit, comment=:comment "
                    "WHERE id=:lid"
                ),
                {"mn": line.material_name, "lwn": line.length_weight_nos,
                 "cost": line.per_unit_cost, "unit": line.unit, "comment": line.comment, "lid": line.id},
            )
        else:
            db.execute(
                text(
                    "INSERT INTO purchase_order_lines "
                    "(purchase_order_id, material_name, length_weight_nos, per_unit_cost, unit, comment) "
                    "VALUES (:pid, :mn, :lwn, :cost, :unit, :comment)"
                ),
                {"pid": po_id, "mn": line.material_name, "lwn": line.length_weight_nos,
                 "cost": line.per_unit_cost, "unit": line.unit, "comment": line.comment},
            )

    db.execute(
        text(
            "UPDATE purchase_orders "
            "SET supplier_id=:sid, order_delivery_date=:deldate, gst_rate=:gst, "
            "delivery_details=CAST(:details AS jsonb), remarks=:remarks, "
            "total_amount=:total, updated_at=now() WHERE id=:id"
        ),
        {
            "sid": sid, "deldate": body.order_delivery_date, "gst": body.gst_rate,
            "details": json.dumps(body.delivery_details or {}), "remarks": body.remarks,
            "total": new_total, "id": po_id,
        },
    )
    db.commit()
    return _serialize_po(db, po_id)


class PoStatusBody(BaseModel):
    status: int

@router.patch("/{po_id}/status")
def patch_po_status(
    po_id: int,
    body: PoStatusBody,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    current_status = db.execute(
        text("SELECT status FROM purchase_orders WHERE id = :id FOR UPDATE"), {"id": po_id}
    ).scalar()
    
    if current_status is None:
        raise HTTPException(404, "Purchase order not found")

    if current_status != 4 and body.status == 4:
        po_lines = db.execute(
            text("SELECT id, material_name, length_weight_nos, delivered_qty, unit, per_unit_cost FROM purchase_order_lines WHERE purchase_order_id = :id"),
            {"id": po_id}
        ).mappings().all()

        for line in po_lines:
            mat_name = line["material_name"].strip()
            total_qty = float(line["length_weight_nos"])
            del_qty = float(line["delivered_qty"] or 0)
            pending_qty = total_qty - del_qty

            if pending_qty > 0:
                # Add to inventory — case-insensitive match so "Widget A" and "widget a" merge
                updated = db.execute(
                    text(
                        "UPDATE materials SET length_weight_nos = length_weight_nos + :qty, "
                        "per_unit_cost = :cost, updated_at = now() "
                        "WHERE LOWER(TRIM(name)) = LOWER(TRIM(:name)) RETURNING id"
                    ),
                    {"qty": pending_qty, "name": mat_name, "cost": float(line["per_unit_cost"])}
                ).first()
                if not updated:
                    db.execute(
                        text("INSERT INTO materials (name, length_weight_nos, unit, per_unit_cost) VALUES (:name, :qty, :unit, :cost)"),
                        {"name": mat_name, "qty": pending_qty, "unit": line["unit"] or "", "cost": float(line["per_unit_cost"])}
                    )
                # Mark as fully delivered in lines
                db.execute(
                    text("UPDATE purchase_order_lines SET delivered_qty = :qty WHERE id = :line_id"),
                    {"qty": total_qty, "line_id": line["id"]}
                )

    db.execute(
        text("UPDATE purchase_orders SET status = :s, updated_at = now() WHERE id = :id RETURNING id"),
        {"s": body.status, "id": po_id},
    )
    db.commit()
    return _serialize_po(db, po_id)


class PoReceiveItem(BaseModel):
    line_id: int
    receive_qty: float = Field(ge=0)

class PoReceiveBody(BaseModel):
    items: List[PoReceiveItem]
    bill_numbers_to_add: List[str] = Field(default_factory=list)

@router.post("/{po_id}/receive")
def receive_po_items(
    po_id: int,
    body: PoReceiveBody,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    po_row = db.execute(
        text("SELECT status, bill_numbers FROM purchase_orders WHERE id = :id FOR UPDATE"), {"id": po_id}
    ).mappings().first()

    if po_row is None:
        raise HTTPException(404, "Purchase order not found")

    current_status = po_row["status"]

    lines_map = {row["id"]: dict(row) for row in db.execute(
        text("SELECT id, material_name, length_weight_nos, delivered_qty, unit, per_unit_cost FROM purchase_order_lines WHERE purchase_order_id = :id"),
        {"id": po_id}
    ).mappings().all()}

    for item in body.items:
        if item.receive_qty <= 0: continue
        line = lines_map.get(item.line_id)
        if not line: continue

        mat_name = line["material_name"].strip()
        receive_qty = float(item.receive_qty)

        # Update inventory — case-insensitive match so "Widget A" and "widget a" merge
        updated = db.execute(
            text(
                "UPDATE materials SET length_weight_nos = length_weight_nos + :qty, "
                "per_unit_cost = :cost, updated_at = now() "
                "WHERE LOWER(TRIM(name)) = LOWER(TRIM(:name)) RETURNING id"
            ),
            {"qty": receive_qty, "name": mat_name, "cost": float(line["per_unit_cost"])}
        ).first()
        if not updated:
            db.execute(
                text("INSERT INTO materials (name, length_weight_nos, unit, per_unit_cost) VALUES (:name, :qty, :unit, :cost)"),
                {"name": mat_name, "qty": receive_qty, "unit": line["unit"] or "", "cost": float(line["per_unit_cost"])}
            )

        # Update line delivered_qty
        new_del = float(line["delivered_qty"] or 0) + receive_qty
        db.execute(
            text("UPDATE purchase_order_lines SET delivered_qty = :qty WHERE id = :line_id"),
            {"qty": new_del, "line_id": item.line_id}
        )
        lines_map[item.line_id]["delivered_qty"] = new_del

    # Append new bill/challan numbers (deduplicated)
    new_bns = [b.strip() for b in body.bill_numbers_to_add if b.strip()]
    if new_bns:
        existing = [x.strip() for x in (po_row["bill_numbers"] or "").split(",") if x.strip()]
        for bn in new_bns:
            if bn not in existing:
                existing.append(bn)
        db.execute(
            text("UPDATE purchase_orders SET bill_numbers = :bn, updated_at = now() WHERE id = :id"),
            {"bn": ",".join(existing), "id": po_id}
        )

    # Auto-flip status: 3 = Partially Delivered, 4 = Delivered
    all_delivered = all(
        float(line["delivered_qty"] or 0) >= float(line["length_weight_nos"])
        for line in lines_map.values()
    )
    new_status = 4 if all_delivered else 3
    if current_status != 4 and current_status != new_status:
        db.execute(
            text("UPDATE purchase_orders SET status = :s, updated_at = now() WHERE id = :id"),
            {"s": new_status, "id": po_id}
        )

    db.commit()
    return _serialize_po(db, po_id)


class AdditionalCostItem(BaseModel):
    label: str = Field(min_length=1)
    amount: float = Field(ge=0)

class AdditionalCostsBody(BaseModel):
    items: List[AdditionalCostItem] = Field(default_factory=list)

@router.patch("/{po_id}/additional-costs")
def update_additional_costs(po_id: int, body: AdditionalCostsBody, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    if not db.execute(text("SELECT id FROM purchase_orders WHERE id = :id"), {"id": po_id}).first():
        raise HTTPException(404, "Purchase order not found")
    lines_total = db.execute(
        text("SELECT COALESCE(SUM(length_weight_nos * per_unit_cost), 0) FROM purchase_order_lines WHERE purchase_order_id = :id"),
        {"id": po_id},
    ).scalar()
    extra = sum(float(i.amount) for i in body.items)
    new_total = float(lines_total or 0) + extra
    items_json = json.dumps([{"label": i.label, "amount": i.amount} for i in body.items])
    db.execute(
        text("UPDATE purchase_orders SET additional_costs = CAST(:ac AS jsonb), total_amount = :total, updated_at = now() WHERE id = :id"),
        {"ac": items_json, "total": new_total, "id": po_id},
    )
    db.commit()
    return _serialize_po(db, po_id)


@router.delete("/{po_id}", status_code=204)
def delete_po(
    po_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    row = db.execute(
        text("SELECT status FROM purchase_orders WHERE id = :id"), {"id": po_id}
    ).first()
    if not row:
        raise HTTPException(404, "Purchase order not found")
    if row[0] == 4:
        raise HTTPException(400, "Delivered purchase orders cannot be deleted")
    db.execute(text("DELETE FROM purchase_order_lines WHERE purchase_order_id = :id"), {"id": po_id})
    db.execute(text("DELETE FROM purchase_orders WHERE id = :id"), {"id": po_id})
    db.commit()


@router.get("/{po_id}/pdf")
def download_po_pdf(
    po_id: int,
    variant: str = "normal",
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    po = _serialize_po(db, po_id)

    pdf_data = {
        "PurchaseDate": str(po.get("purchase_date") or ""),
        "PurchaseNumber": po["purchase_number"],
        "PurchaseOrderDeliveryDate": str(po.get("order_delivery_date") or ""),
        "ActualDeliveryDate": str(po["actual_delivery_date"]) if po.get("actual_delivery_date") else "",
        "BillNumbers": po.get("bill_numbers") or "",
        "Vendor Name": po.get("supplier_name") or "",
        "Vendor Address": po.get("supplier_location") or "",
        "Vendor Phone": po.get("supplier_contact") or "",
        "Vendor GSTIN Number": po.get("supplier_gstin") or "N/A",
        "Materials": [
            {
                "name": line["material_name"],
                "length_weight_nos": float(line["length_weight_nos"]),
                "per_unit_cost": float(line["per_unit_cost"]),
                "unit": line.get("unit") or "Nos",
                "comment": line.get("comment") or "",
            }
            for line in po["lines"]
        ],
        "PurchaseGST": float(po.get("gst_rate") or 18),
        "AdditionalCosts": po.get("additional_costs") or [],
        "DeliveryDetails": po.get("delivery_details") or {},
        "Variant": variant,
    }

    buf = _pdf_svc.generate_purchase_order_pdf(pdf_data)

    import re
    def _safe(s: str) -> str:
        return re.sub(r"[^\w\-]", "_", s).strip("_")

    po_num = _safe(po["purchase_number"])
    supplier = _safe(po.get("supplier_name") or "Unknown")
    po_date = _safe(str(po.get("purchase_date") or ""))
    filename = f"{po_num}-{supplier}-{po_date}.pdf"

    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
