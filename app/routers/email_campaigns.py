from __future__ import annotations

import csv
import io
import os
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..config import settings
from .. import email_service

# Uploaded images are stored alongside the backend package
_UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploaded_images"

router = APIRouter(prefix="/email-campaigns", tags=["email-campaigns"])

# ── DB bootstrap ───────────────────────────────────────────────────────────────
_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS email_campaigns (
    id                SERIAL PRIMARY KEY,
    subject           TEXT NOT NULL,
    body_html         TEXT NOT NULL,
    reply_to          TEXT,
    cc                TEXT,
    bcc               TEXT,
    status            VARCHAR(20) DEFAULT 'running',
    total_recipients  INTEGER DEFAULT 0,
    sent_count        INTEGER DEFAULT 0,
    failed_count      INTEGER DEFAULT 0,
    created_at        TIMESTAMP DEFAULT now(),
    updated_at        TIMESTAMP DEFAULT now(),
    completed_at      TIMESTAMP
);
CREATE TABLE IF NOT EXISTS email_campaign_failures (
    id           SERIAL PRIMARY KEY,
    campaign_id  INTEGER REFERENCES email_campaigns(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,
    error        TEXT,
    created_at   TIMESTAMP DEFAULT now()
);
"""


def _ensure_tables(db: Session):
    db.execute(text(_SETUP_SQL))
    db.commit()


# ── Models ─────────────────────────────────────────────────────────────────────

class CampaignCreate(BaseModel):
    subject: str = Field(min_length=1)
    body_html: str = Field(min_length=1)
    recipients: List[str] = Field(min_length=1)
    reply_to: Optional[str] = None
    cc: Optional[str] = None
    bcc: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_emails_from_csv(content: bytes) -> List[str]:
    text_content = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text_content))
    emails: List[str] = []
    email_col = None
    for row in reader:
        if email_col is None:
            # Find the email column (case-insensitive)
            for col in row.keys():
                if "email" in col.lower() or "mail" in col.lower():
                    email_col = col
                    break
            if email_col is None and row:
                email_col = list(row.keys())[0]
        if email_col and row.get(email_col, "").strip():
            val = row[email_col].strip().lower()
            if "@" in val:
                emails.append(val)
    return emails


def _extract_emails_from_xlsx(content: bytes) -> List[str]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    emails: List[str] = []
    email_col_idx: Optional[int] = None
    for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
        if row_idx == 0:
            # Header row — find email column
            for ci, cell in enumerate(row):
                if cell and ("email" in str(cell).lower() or "mail" in str(cell).lower()):
                    email_col_idx = ci
                    break
            if email_col_idx is None:
                email_col_idx = 0  # fall back to first column
            continue
        if row and email_col_idx is not None and row[email_col_idx]:
            val = str(row[email_col_idx]).strip().lower()
            if "@" in val:
                emails.append(val)
    wb.close()
    return emails


# ── Endpoints ──────────────────────────────────────────────────────────────────

_MAX_IMAGE_PX = 800   # max width/height in pixels
_JPEG_QUALITY = 85    # JPEG compression quality


def _compress_image(raw: bytes, ext: str) -> tuple[bytes, str]:
    """Resize to max 800 px and re-encode as JPEG. Returns (bytes, new_ext)."""
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(raw))
        img = img.convert("RGB")
        w, h = img.size
        if w > _MAX_IMAGE_PX or h > _MAX_IMAGE_PX:
            ratio = _MAX_IMAGE_PX / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        return buf.getvalue(), ".jpg"
    except Exception:
        return raw, ext  # fall back to original if Pillow fails


@router.post("/upload-image", status_code=201)
async def upload_image(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload an image for use inside email bodies. Returns an absolute URL."""
    _ = user
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "image.jpg").suffix.lower() or ".jpg"
    allowed = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported image type: {ext}")
    raw = await file.read()
    # GIFs are kept as-is; everything else is compressed to JPEG
    if ext not in (".gif",):
        raw, ext = _compress_image(raw, ext)
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = _UPLOAD_DIR / filename
    dest.write_bytes(raw)
    return {"url": f"/api/v1/email-campaigns/images/{filename}"}


@router.get("/images/{filename}")
def serve_image(filename: str):
    """Serve a previously uploaded campaign image."""
    # Prevent path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    path = _UPLOAD_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(str(path))


@router.delete("/{campaign_id}", status_code=204)
def delete_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Delete a campaign record (and its failure log via CASCADE)."""
    _ = user
    _ensure_tables(db)
    row = db.execute(
        text("SELECT status FROM email_campaigns WHERE id = :id"),
        {"id": campaign_id},
    ).first()
    if not row:
        raise HTTPException(404, "Campaign not found")
    if row[0] == "running":
        raise HTTPException(409, "Cannot delete a running campaign. Stop it first.")
    db.execute(text("DELETE FROM email_campaigns WHERE id = :id"), {"id": campaign_id})
    db.commit()


@router.get("")
def list_campaigns(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    _ensure_tables(db)
    rows = db.execute(
        text(
            "SELECT id, subject, reply_to, cc, bcc, status, total_recipients, "
            "sent_count, failed_count, created_at, completed_at "
            "FROM email_campaigns ORDER BY id DESC LIMIT 50"
        )
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/contacts")
def get_contacts(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    rows = db.execute(
        text("SELECT name, email FROM companies WHERE email IS NOT NULL AND email <> '' ORDER BY name")
    ).mappings().all()
    return [dict(r) for r in rows]


@router.post("/parse-contacts")
async def parse_contacts(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    _ = user
    content = await file.read()
    filename = (file.filename or "").lower()

    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        emails = _extract_emails_from_xlsx(content)
    elif filename.endswith(".csv") or filename.endswith(".txt"):
        emails = _extract_emails_from_csv(content)
    else:
        # Try CSV as fallback
        emails = _extract_emails_from_csv(content)

    # Deduplicate
    seen: dict[str, bool] = {}
    unique = [e for e in emails if not seen.get(e) and not seen.update({e: True})]  # type: ignore[func-returns-value]
    return {"emails": unique, "count": len(unique)}


@router.get("/active")
def get_active(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    _ensure_tables(db)
    row = db.execute(
        text(
            "SELECT id, subject, status, total_recipients, sent_count, failed_count, created_at "
            "FROM email_campaigns WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        )
    ).mappings().first()
    return {"running": email_service.is_running(), "campaign": dict(row) if row else None}


@router.post("", status_code=201)
def create_campaign(
    body: CampaignCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _ = user
    _ensure_tables(db)

    if email_service.is_running():
        raise HTTPException(409, "A campaign is already running. Stop it first.")

    recipients = list(dict.fromkeys(e.strip().lower() for e in body.recipients if "@" in e.strip()))
    if not recipients:
        raise HTTPException(400, "No valid email addresses provided.")

    row = db.execute(
        text(
            "INSERT INTO email_campaigns "
            "(subject, body_html, reply_to, cc, bcc, status, total_recipients) "
            "VALUES (:subject, :body, :reply_to, :cc, :bcc, 'running', :total) RETURNING id"
        ),
        {
            "subject": body.subject,
            "body": body.body_html,
            "reply_to": body.reply_to or None,
            "cc": body.cc or None,
            "bcc": body.bcc or None,
            "total": len(recipients),
        },
    ).first()
    db.commit()
    campaign_id = row[0]

    email_service.start_campaign(
        campaign_id, body.subject, body.body_html, recipients,
        reply_to=body.reply_to or None,
        cc=body.cc or None,
        bcc=body.bcc or None,
    )
    return {"id": campaign_id, "total_recipients": len(recipients)}


@router.post("/{campaign_id}/stop")
def stop_campaign(campaign_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    email_service.stop_campaign()
    return {"stopped": True}


@router.get("/{campaign_id}/failures")
def get_failures(campaign_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    _ = user
    rows = db.execute(
        text("SELECT email, error, created_at FROM email_campaign_failures WHERE campaign_id = :id ORDER BY id"),
        {"id": campaign_id},
    ).mappings().all()
    return [dict(r) for r in rows]
