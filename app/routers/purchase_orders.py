from __future__ import annotations

import json
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

_pdf_svc = PDFGenerationService()

router = APIRouter(prefix="/purchase-orders", tags=["purchase-orders"])


class PoLineIn(BaseModel):
    material_name: str = Field(min_length=1)
    length_weight_nos: float = Field(gt=0)
    per_unit_cost: float = Field(ge=0)
    unit: Optional[str] = None
    comment: Optional[str] = None


class PurchaseOrderCreate(BaseModel):
    purchase_number: str = Field(min_length=1)
    supplier_name: str = Field(min_length=1)
    supplier_location: str = ""
    supplier_contact: str = ""
    supplier_gstin: Optional[str] = None
    order_delivery_date: Optional[date] = None
    gst_rate: float = Field(default=18, ge=0)
    delivery_details: dict = Field(default_factory=dict)
    lines: List[PoLineIn] = Field(min_length=1)


def _supplier_id(db: Session, body: PurchaseOrderCreate) -> int:
    row = db.execute(
        text("SELECT id FROM suppliers WHERE name = :n"),
        {"n": body.supplier_name.strip()},
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
            "name": body.supplier_name.strip(),
            "loc": body.supplier_location or "",
            "contact": body.supplier_contact or "",
            "gstin": body.supplier_gstin,
        },
    ).first()
    return ins[0]


def _serialize_po(db: Session, po_id: int) -> dict:
    head = db.execute(
        text(
            """
            SELECT p.id, p.purchase_number, p.purchase_date, p.total_amount, p.order_delivery_date,
                   p.status, p.gst_rate, p.actual_delivery_date, p.bill_numbers, p.delivery_details,
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
    d["lines"] = [dict(x) for x in lines]
    return d


@router.get("")
def list_pos(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    rows = db.execute(
        text(
            """
            SELECT p.id, p.purchase_number, p.total_amount, p.status, p.order_delivery_date, p.created_at,
                   s.name AS supplier_name
            FROM purchase_orders p
            LEFT JOIN suppliers s ON s.id = p.supplier_id
            ORDER BY p.created_at DESC NULLS LAST
            """
        )
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
    sid = _supplier_id(db, body)

    row = db.execute(
        text(
            """
            INSERT INTO purchase_orders (
              purchase_number, supplier_id, purchase_reference, total_amount,
              order_delivery_date, status, gst_rate, delivery_details
            )
            VALUES (:num, :sid, '', :total, :deldate, 1, :gst, CAST(:details AS jsonb))
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
                # Add to inventory
                updated = db.execute(
                    text("UPDATE materials SET length_weight_nos = length_weight_nos + :qty WHERE name = :name RETURNING id"),
                    {"qty": pending_qty, "name": mat_name}
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

@router.post("/{po_id}/receive")
def receive_po_items(
    po_id: int,
    body: PoReceiveBody,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    current_status = db.execute(
        text("SELECT status FROM purchase_orders WHERE id = :id FOR UPDATE"), {"id": po_id}
    ).scalar()
    
    if current_status is None:
        raise HTTPException(404, "Purchase order not found")

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

        # Update inventory
        updated = db.execute(
            text("UPDATE materials SET length_weight_nos = length_weight_nos + :qty WHERE name = :name RETURNING id"),
            {"qty": receive_qty, "name": mat_name}
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

    # Check if we should automatically flip status to Delivered (4)
    all_delivered = True
    for line in lines_map.values():
        if float(line["delivered_qty"] or 0) < float(line["length_weight_nos"]):
            all_delivered = False
            break

    new_status = 4 if all_delivered else 3 # 3 is Partially Delivered
    if current_status != 4 and current_status != new_status:
        db.execute(
            text("UPDATE purchase_orders SET status = :s, updated_at = now() WHERE id = :id"),
            {"s": new_status, "id": po_id}
        )

    db.commit()
    return _serialize_po(db, po_id)


@router.get("/{po_id}/pdf")
def download_po_pdf(
    po_id: int,
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
        "DeliveryDetails": po.get("delivery_details") or {},
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
