from __future__ import annotations

import json
import logging
import mimetypes
import re
import urllib.error
import urllib.request
from datetime import date
from html import escape
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import cloudinary_service
from ..db import get_db
from ..deps import get_current_user, require_module
from ..email_service import send_transactional_email
from ..upload_limits import read_limited

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sales-orders", tags=["sales-orders"])

DocumentKind = Literal["invoice", "eway_bill", "lr_copy"]
_DOCUMENT_COLUMN = {
    "invoice": "invoice_document",
    "eway_bill": "eway_bill_document",
    "lr_copy": "lr_copy_document",
}


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
    # Metadata returned by POST /sales-orders/upload-document — never a URL,
    # since an authenticated Cloudinary asset has no permanent one. Both
    # optional: each document can be attached after creation too.
    invoice_document: Optional[dict] = None
    eway_bill_document: Optional[dict] = None
    lr_copy_document: Optional[dict] = None


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
    invoice_document: Optional[dict] = None
    eway_bill_document: Optional[dict] = None
    lr_copy_document: Optional[dict] = None


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_DOCUMENT_LABELS = {
    "invoice_document": "Sales Invoice",
    "eway_bill_document": "E-Way Bill",
    "lr_copy_document": "LR Copy",
}


class SendEmailBody(BaseModel):
    to_email: str = Field(min_length=3)
    force: bool = False
    cc_emails: Optional[str] = None
    bcc_emails: Optional[str] = None

    @field_validator("to_email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        v = v.strip()
        if not _EMAIL_RE.match(v):
            raise ValueError("Enter a valid email address")
        return v

    @field_validator("cc_emails", "bcc_emails")
    @classmethod
    def _validate_multi_email(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        for addr in v.split(","):
            addr = addr.strip()
            if addr and not _EMAIL_RE.match(addr):
                raise ValueError(f"Invalid email address: {addr}")
        return v


def _fmt_money(amount: float) -> str:
    return f"₹{amount:,.2f}"


def _build_so_email_html(so: dict, attached_labels: List[str]) -> tuple[str, str]:
    """
    Returns (subject, html) for the sales-order document email. `so` is the
    dict shape _serialize_so() returns; `attached_labels` are the document
    labels actually being attached (may be fewer than 3 if the sender chose
    to proceed with some missing).
    """
    company = escape(so.get("company_name") or "the customer")
    invoice_number = escape(str(so["invoice_number"]))

    rows = []
    for line in so["lines"]:
        name = escape(str(line["product_name"]))
        code = escape(str(line["product_code"])) if line.get("product_code") else ""
        label = f"{name}{f' ({code})' if code else ''}"
        qty = line["quantity_sold"]
        unit_price = _fmt_money(float(line["unit_price"]))
        total = _fmt_money(float(line["total_price"]))
        rows.append(
            f"<tr>"
            f"<td style='padding:4px 12px 4px 0'>{label}</td>"
            f"<td style='padding:4px 12px;text-align:right'>{qty}</td>"
            f"<td style='padding:4px 12px;text-align:right'>{unit_price}</td>"
            f"<td style='padding:4px 12px;text-align:right'>{total}</td>"
            f"</tr>"
        )
    items_table = (
        f"<table style='margin:12px 0;font-size:14px;width:100%;border-collapse:collapse'>"
        f"<tr style='font-weight:600;color:#555;border-bottom:1px solid #e2e2e2'>"
        f"<td style='padding:4px 12px 4px 0'>Item</td>"
        f"<td style='padding:4px 12px;text-align:right'>Qty</td>"
        f"<td style='padding:4px 12px;text-align:right'>Unit Price</td>"
        f"<td style='padding:4px 12px;text-align:right'>Total</td>"
        f"</tr>{''.join(rows)}</table>"
    )

    docs_html = "".join(f"<li>{escape(label)}</li>" for label in attached_labels)
    docs_sentence = (
        f"<p>Attached to this email {'is' if len(attached_labels) == 1 else 'are'} the "
        f"following document{'s' if len(attached_labels) != 1 else ''} for this order:</p>"
        f"<ul>{docs_html}</ul>"
        if attached_labels
        else "<p>No supporting documents were available to attach to this email.</p>"
    )

    subject = f"Sales Order {invoice_number} — Documents from E-SAFE Enterprises"
    total_amount = _fmt_money(float(so["total_amount"]))

    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#222">
      <p>Dear {company},</p>
      <p>
        Please find below the details of Sales Order <strong>{invoice_number}</strong>,
        along with the associated documents.
      </p>
      {items_table}
      <p style="margin:0 0 12px"><strong>Order Total: {total_amount}</strong></p>
      {docs_sentence}
      <p>Please feel free to reach out with any questions regarding this order.</p>
      <p>
        Best regards,<br>
        <strong>E-SAFE Enterprises</strong><br>
        <span style="color:#666;font-size:13px">
          G-176, Boranada Industrial Park, Jodhpur-342012 (RAJ.)<br>
          Phone: +91 291 2944321 · Mobile: +91 94133 24321<br>
          Email: esafe@esafe.co.in · www.esafe.co.in
        </span>
      </p>
    </div>
    """.strip()

    return subject, html


def _fetch_document_bytes(document: dict) -> Optional[tuple[str, bytes, str]]:
    """
    Downloads a sales-order document's bytes via a freshly generated
    Cloudinary signed URL, for attaching to an outbound email.

    Returns (filename, bytes, mime_type), or None if the download fails —
    a single unreachable document should not block the whole email from
    going out with the others attached.
    """
    try:
        url = cloudinary_service.get_signed_url(document)
        with urllib.request.urlopen(url, timeout=30) as resp:
            raw = resp.read()
    except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
        logger.warning("Could not download document %r for emailing: %s", document.get("public_id"), exc)
        return None

    fmt = (document.get("format") or "").lower()
    filename = document.get("original_filename") or f"document.{fmt or 'pdf'}"
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return filename, raw, mime_type


def _serialize_so(db: Session, so_id: int) -> dict:
    head = db.execute(
        text(
            """
            SELECT id, invoice_number, company_name, company_location, company_contact, company_gstin,
                   sales_date, delivery_date, actual_delivery_date, total_amount, status, gst_rate,
                   payment_status, dispatch_status,
                   delivery_details, notes, created_at, updated_at, payment_received, payment_amount,
                   COALESCE(additional_costs, '[]'::jsonb) AS additional_costs,
                   invoice_document, eway_bill_document, lr_copy_document
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
    for key in ("invoice_document", "eway_bill_document", "lr_copy_document"):
        if isinstance(d.get(key), str):
            d[key] = json.loads(d[key])
    d["lines"] = [dict(x) for x in lines]
    subtotal = sum(float(x["total_price"]) for x in d["lines"])
    d["subtotal"] = subtotal
    d["gst_amount"] = subtotal * (float(d["gst_rate"]) / 100.0)
    return d


@router.post("/upload-document", dependencies=[Depends(require_module("sales_orders"))])
async def upload_document(
    doc_type: DocumentKind,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """
    Upload a sales-invoice, e-way-bill, or LR-copy file to Cloudinary and
    return its identity metadata (never a URL — see cloudinary_service
    module docstring).

    Deliberately not scoped to an existing sales_order_id: a document can be
    attached while the order is still being composed in the create form,
    before it has an id, matching the campaign-image upload pattern already
    used elsewhere in this app (upload first, get metadata back, include it
    in the create payload).

    Two performance measures, both explained fully in cloudinary_service:
      - Raster images (a phone photo of a paper LR copy, say) are resized and
        re-compressed before upload, at a resolution generous enough to stay
        legible. PDFs pass through untouched — see
        cloudinary_service.optimize_if_image for why PDFs are not
        recompressed at all.
      - The actual Cloudinary call is synchronous (the SDK has no async
        client) and is offloaded to a thread pool rather than awaited
        directly, so one in-flight upload does not stall every other request
        this server is handling concurrently.
    """
    _ = user
    raw = await read_limited(file)
    filename = file.filename or f"{doc_type}.pdf"
    # CPU-bound (resize + JPEG re-encode), not I/O-bound — still worth the
    # thread-pool hop so a large image doesn't block the event loop either.
    raw = await run_in_threadpool(cloudinary_service.optimize_if_image, raw, filename)
    try:
        document = await run_in_threadpool(
            cloudinary_service.upload_document,
            raw,
            filename=filename,
            folder="sales-order-documents",
        )
    except RuntimeError as exc:
        # Cloudinary not configured — a deployment/env issue, not the caller's fault.
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Upload to Cloudinary failed: {exc}") from exc
    return {"doc_type": doc_type, "document": document}


@router.get(
    "/{so_id}/documents/{doc_type}/url",
    dependencies=[Depends(require_module("sales_orders"))],
)
def get_document_url(so_id: int, doc_type: DocumentKind, db: Session = Depends(get_db)):
    """
    Generate a fresh signed URL for a previously-attached document.

    Called on demand, right before the frontend opens the link — the URL is
    time-limited (cloudinary_service.SIGNED_URL_TTL_SECONDS) and must never
    be cached or persisted on either side.
    """
    column = _DOCUMENT_COLUMN[doc_type]
    row = db.execute(
        text(f"SELECT {column} AS doc FROM sales_orders WHERE id = :id"),
        {"id": so_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(404, "Sales order not found")
    document = row["doc"]
    if isinstance(document, str):
        document = json.loads(document)
    if not document:
        raise HTTPException(404, f"No {doc_type.replace('_', ' ')} attached to this order.")
    try:
        url = cloudinary_service.get_signed_url(document)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"url": url, "expires_in_seconds": cloudinary_service.SIGNED_URL_TTL_SECONDS}


@router.post(
    "/{so_id}/send-email",
    dependencies=[Depends(require_module("sales_orders"))],
)
def send_email(
    so_id: int,
    body: SendEmailBody,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Emails the sales order's item list plus whichever of the three
    documents (invoice, e-way bill, LR copy) are attached, to `to_email`.

    Two-step confirmation, driven by `force`:
      - force=False (the initial call): if any of the three documents is
        missing, nothing is sent — the response reports which are missing
        so the caller can ask "send anyway?" before retrying.
      - force=True: sends regardless of what's missing, attaching only the
        documents that are actually present.
    """
    _ = user
    so = _serialize_so(db, so_id)

    missing = [label for key, label in _DOCUMENT_LABELS.items() if not so.get(key)]
    if missing and not body.force:
        return {"status": "missing_documents", "missing": missing}

    attachments = []
    attached_labels = []
    for key, label in _DOCUMENT_LABELS.items():
        document = so.get(key)
        if not document:
            continue
        fetched = _fetch_document_bytes(document)
        if fetched is None:
            continue
        attachments.append(fetched)
        attached_labels.append(label)

    subject, html = _build_so_email_html(so, attached_labels)

    # Merge the user-supplied bcc with the fixed internal bcc address.
    internal_bcc = "accounts@esafe.co.in"
    if body.bcc_emails and body.bcc_emails.strip():
        merged_bcc = f"{internal_bcc},{body.bcc_emails}"
    else:
        merged_bcc = internal_bcc

    try:
        send_transactional_email(
            body.to_email, subject, html,
            attachments=attachments,
            cc=body.cc_emails or None,
            bcc=merged_bcc,
        )
    except Exception as exc:
        raise HTTPException(502, f"Failed to send email: {exc}") from exc

    return {"status": "sent", "attached": attached_labels}


@router.get("")
def list_so(
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    conds = []
    params: dict = {}
    if date_from:
        conds.append("created_at::date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        conds.append("created_at::date <= :date_to")
        params["date_to"] = date_to
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = db.execute(
        text(f"""
            SELECT id, invoice_number, company_name, total_amount, status,
                   payment_status, dispatch_status,
                   sales_date, created_at, payment_received, payment_amount
            FROM sales_orders {where}
            ORDER BY created_at DESC NULLS LAST
        """),
        params,
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


@router.post("", status_code=201, dependencies=[Depends(require_module("sales_orders"))])
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

    # finished_goods rows this order draws stock from, so the zero-stock
    # cleanup at the end can be scoped to them instead of running globally.
    touched_fg_ids: set[int] = set()

    subtotal = sum(t for t, _ in line_totals)
    gst_amt = subtotal * (body.gst_rate / 100.0)
    total = subtotal + gst_amt

    row = db.execute(
        text(
            """
            INSERT INTO sales_orders (
              invoice_number, company_name, company_location, company_contact, company_gstin,
              sales_date, delivery_date, total_amount, status, payment_status, dispatch_status,
              gst_rate, delivery_details, notes, invoice_document, eway_bill_document, lr_copy_document
            )
            VALUES (
              :inv, :cname, :cloc, :ccon, :gstin, :sdate, :ddate, :total, 1, 1, :dispatch,
              :grate, CAST(:details AS jsonb), :notes,
              CAST(:invoice_doc AS jsonb), CAST(:eway_doc AS jsonb), CAST(:lr_doc AS jsonb)
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
            # Lines below are inserted with dispatched_qty = quantity_sold and
            # finished-goods stock is deducted immediately, so the goods have
            # already left inventory at creation: dispatch_status = 4 (full).
            # This mirrors the existing stock model rather than changing it.
            "dispatch": 4,
            "grate": body.gst_rate,
            "details": json.dumps(body.delivery_details or {}),
            "notes": body.notes,
            "invoice_doc": json.dumps(body.invoice_document) if body.invoice_document else None,
            "eway_doc": json.dumps(body.eway_bill_document) if body.eway_bill_document else None,
            "lr_doc": json.dumps(body.lr_copy_document) if body.lr_copy_document else None,
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
            touched_fg_ids.add(int(fgid))
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
                touched_fg_ids.add(int(fg["id"]))
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

    # Clear out finished-goods rows this order drained to zero.
    #
    # This used to run unscoped — `DELETE FROM finished_goods WHERE
    # quantity_in_stock <= 0` with no reference to the order — so creating one
    # sales order deleted every zero-stock finished-goods row in the database,
    # including rows belonging to unrelated orders and work orders. Now limited
    # to the rows this order actually drew from.
    if touched_fg_ids:
        fg_ids = sorted(touched_fg_ids)
        db.execute(
            text(
                "UPDATE sales_order_items SET finished_good_id = NULL "
                "WHERE finished_good_id = ANY(:ids) AND finished_good_id IN "
                "(SELECT id FROM finished_goods WHERE quantity_in_stock <= 0)"
            ),
            {"ids": fg_ids},
        )
        db.execute(
            text(
                "DELETE FROM finished_goods "
                "WHERE id = ANY(:ids) AND quantity_in_stock <= 0"
            ),
            {"ids": fg_ids},
        )

    db.commit()
    return _serialize_so(db, so_id)


@router.patch("/{so_id}", dependencies=[Depends(require_module("sales_orders"))])
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
            "delivery_details=CAST(:details AS jsonb), notes=:notes, total_amount=:total, "
            "invoice_document=CAST(:invoice_doc AS jsonb), eway_bill_document=CAST(:eway_doc AS jsonb), "
            "lr_copy_document=CAST(:lr_doc AS jsonb), "
            "updated_at=now() "
            "WHERE id=:id"
        ),
        {
            "cname": body.company_name.strip(), "cloc": body.company_location, "ccon": body.company_contact,
            "gstin": body.company_gstin, "sdate": body.sales_date, "ddate": body.delivery_date,
            "grate": body.gst_rate, "details": json.dumps(body.delivery_details or {}),
            "notes": body.notes, "total": new_total, "id": so_id,
            "invoice_doc": json.dumps(body.invoice_document) if body.invoice_document else None,
            "eway_doc": json.dumps(body.eway_bill_document) if body.eway_bill_document else None,
            "lr_doc": json.dumps(body.lr_copy_document) if body.lr_copy_document else None,
        },
    )
    db.commit()
    return _serialize_so(db, so_id)


class PaymentUpdate(BaseModel):
    # 1 = Not Received, 2 = Partially Received, 3 = Received
    payment_status: int = Field(ge=1, le=3)
    payment_amount: Optional[float] = Field(default=None, ge=0)

@router.patch("/{so_id}/payment", dependencies=[Depends(require_module("sales_orders"))])
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
    # Writes payment_status (authoritative) and keeps the legacy `status`
    # column mirrored so any reader not yet migrated keeps working. Dispatch
    # no longer touches `status`, so the two can no longer overwrite each
    # other. payment_received is derived here — it existed in the schema from
    # the start but no endpoint ever wrote it.
    db.execute(
        text(
            "UPDATE sales_orders SET payment_status = :st, status = :st, "
            "payment_received = :recv, payment_amount = :amt, updated_at = now() "
            "WHERE id = :id"
        ),
        {
            "st": body.payment_status,
            "recv": body.payment_status == 3,
            "amt": amt,
            "id": so_id,
        },
    )
    db.commit()
    return _serialize_so(db, so_id)


class AdditionalCostItem(BaseModel):
    label: str = Field(min_length=1)
    amount: float = Field(ge=0)

class AdditionalCostsBody(BaseModel):
    items: List[AdditionalCostItem] = Field(default_factory=list)

@router.patch("/{so_id}/additional-costs", dependencies=[Depends(require_module("sales_orders"))])
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

@router.post("/{so_id}/dispatch", dependencies=[Depends(require_module("sales_orders"))])
def dispatch_so(so_id: int, body: DispatchCreate, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    po_row = db.execute(
        text("SELECT id, dispatch_status FROM sales_orders WHERE id = :id FOR UPDATE"),
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

        # Deduct from Finished Goods. Every branch checks sufficiency first —
        # without it a dispatch drove quantity_in_stock negative silently.
        qty_needed = float(item.dispatch_qty)
        if line["finished_good_id"]:
            fg = db.execute(
                text(
                    "SELECT id, product_name, quantity_in_stock FROM finished_goods "
                    "WHERE id = :id FOR UPDATE"
                ),
                {"id": line["finished_good_id"]},
            ).mappings().first()
            if not fg:
                raise HTTPException(
                    400,
                    f"Finished good for '{line['product_name']}' no longer exists in stock.",
                )
            if float(fg["quantity_in_stock"]) < qty_needed:
                raise HTTPException(
                    400,
                    f"Insufficient stock for '{fg['product_name']}': "
                    f"available {float(fg['quantity_in_stock'])}, required {qty_needed}",
                )
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

            available = sum(float(r["quantity_in_stock"]) for r in fg_rows)
            if available < qty_needed:
                raise HTTPException(
                    400,
                    f"Insufficient stock for '{pname}': "
                    f"available {available}, required {qty_needed}",
                )

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

    # Writes dispatch_status only. This used to write `status`, which the
    # payment endpoint also owned — so recording a dispatch silently destroyed
    # the payment state (and vice-versa). The two are now separate columns.
    new_status = po_row["dispatch_status"]
    if all_done:
        new_status = 4  # Full Dispatch
    elif any_done:
        new_status = 3  # Partial Dispatch

    if new_status != po_row["dispatch_status"]:
        db.execute(
            text("UPDATE sales_orders SET dispatch_status = :st, updated_at = now() WHERE id = :id"),
            {"st": new_status, "id": so_id},
        )

    db.commit()
    return _serialize_so(db, so_id)


@router.delete("/{so_id}", status_code=204, dependencies=[Depends(require_module("sales_orders"))])
def delete_so(so_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    if not db.execute(text("SELECT id FROM sales_orders WHERE id = :id"), {"id": so_id}).first():
        raise HTTPException(404, "Sales order not found")
    db.execute(text("DELETE FROM sales_order_items WHERE sales_order_id = :id"), {"id": so_id})
    db.execute(text("DELETE FROM sales_orders WHERE id = :id"), {"id": so_id})
    db.commit()

