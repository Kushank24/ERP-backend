"""Background email sender for bulk campaigns.

Sending backend is chosen at runtime:
  - RESEND_API_KEY set  →  Resend HTTP API (works on Railway / any host)
  - otherwise           →  raw SMTP (local dev)
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import smtplib
import threading
import time
import urllib.error
import urllib.request
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional, Tuple

from .config import settings
from .db import SessionLocal

_IMG_DIR = Path(__file__).resolve().parent.parent / "uploaded_images"

logger = logging.getLogger(__name__)

# ── CID image embedding ───────────────────────────────────────────────────────

_IMG_SRC_RE = re.compile(
    r'src="(https?://[^"]+/email-campaigns/images/([^"?#]+))"',
    re.IGNORECASE,
)


def _embed_images(html: str) -> Tuple[str, List[Tuple[str, bytes, str]], List[Path]]:
    """
    Replace uploaded image URLs with cid: references.
    Returns (modified_html, [(cid, bytes, mime_type)], [file_paths]).
    """
    seen: dict[str, str] = {}
    parts: List[Tuple[str, bytes, str]] = []
    paths: List[Path] = []

    def _replace(m: re.Match) -> str:
        filename = m.group(2)
        if filename in seen:
            return f'src="cid:{seen[filename]}"'
        path = _IMG_DIR / filename
        if not path.exists():
            return m.group(0)
        cid = f"img_{re.sub(r'[^a-zA-Z0-9]', '_', filename)}"
        seen[filename] = cid
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        parts.append((cid, path.read_bytes(), mime))
        paths.append(path)
        return f'src="cid:{cid}"'

    modified = _IMG_SRC_RE.sub(_replace, html)
    return modified, parts, paths


def _delete_image_files(paths: List[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not delete image file %s: %s", p, exc)


# ── Resend HTTP API sender ────────────────────────────────────────────────────

def _send_via_resend(
    to_email: str,
    subject: str,
    html: str,
    inline_images: List[Tuple[str, bytes, str]],
    reply_to: Optional[str],
    cc: Optional[str],
    bcc: Optional[str],
) -> None:
    payload: dict = {
        "from": settings.smtp_user,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    if cc:
        payload["cc"] = [a.strip() for a in cc.split(",") if a.strip()]
    if bcc:
        payload["bcc"] = [a.strip() for a in bcc.split(",") if a.strip()]
    if inline_images:
        payload["attachments"] = [
            {
                "content_id": cid,
                "filename": f"{cid}.jpg",
                "content": base64.b64encode(img_bytes).decode(),
            }
            for cid, img_bytes, _ in inline_images
        ]

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"Resend {resp.status}: {resp.read().decode()[:200]}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:300]
        raise RuntimeError(f"Resend {exc.code}: {body}") from exc


# ── DB helpers ────────────────────────────────────────────────────────────────

_campaign_thread: threading.Thread | None = None
_stop_event = threading.Event()


def is_running() -> bool:
    return _campaign_thread is not None and _campaign_thread.is_alive()


def stop_campaign():
    _stop_event.set()


def _update_db(campaign_id: int, sent_delta: int = 0, failed_delta: int = 0,
               status: str | None = None, error_message: str | None = None):
    from sqlalchemy import text
    try:
        with SessionLocal() as db:
            parts = ["updated_at = now()"]
            params: dict = {"id": campaign_id}
            if sent_delta:
                parts.append("sent_count = sent_count + :sd")
                params["sd"] = sent_delta
            if failed_delta:
                parts.append("failed_count = failed_count + :fd")
                params["fd"] = failed_delta
            if status:
                parts.append("status = :status")
                params["status"] = status
                if status in ("completed", "stopped", "failed"):
                    parts.append("completed_at = now()")
            if error_message:
                parts.append("error_message = :errmsg")
                params["errmsg"] = error_message[:500]
            db.execute(text(f"UPDATE email_campaigns SET {', '.join(parts)} WHERE id = :id"), params)
            db.commit()
    except Exception as exc:
        logger.error("Campaign DB update failed: %s", exc)


def _log_failure(campaign_id: int, email: str, error: str) -> None:
    from sqlalchemy import text
    try:
        with SessionLocal() as db:
            db.execute(
                text(
                    "INSERT INTO email_campaign_failures (campaign_id, email, error) "
                    "VALUES (:cid, :em, :err)"
                ),
                {"cid": campaign_id, "em": email, "err": error[:500]},
            )
            db.commit()
    except Exception:
        pass


# ── Worker ────────────────────────────────────────────────────────────────────

def _send_worker(
    campaign_id: int,
    subject: str,
    body_html: str,
    recipients: List[str],
    reply_to: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
):
    _stop_event.clear()

    # Embed images once — loads bytes from disk, replaces URLs with cid: refs
    html_to_send, inline_images, image_paths = _embed_images(body_html)
    _delete_image_files(image_paths)   # files no longer needed after this point

    use_resend = bool(settings.resend_api_key)

    try:
        if use_resend:
            _run_resend_campaign(
                campaign_id, subject, html_to_send, inline_images,
                recipients, reply_to, cc, bcc,
            )
        else:
            _run_smtp_campaign(
                campaign_id, subject, html_to_send, inline_images,
                recipients, reply_to, cc, bcc,
            )
    except Exception as exc:
        logger.error("Campaign %d worker crashed: %s", campaign_id, exc)
        _update_db(campaign_id, status="failed", error_message=str(exc))


# ── Resend campaign loop ──────────────────────────────────────────────────────

def _run_resend_campaign(
    campaign_id: int,
    subject: str,
    html: str,
    inline_images: List[Tuple[str, bytes, str]],
    recipients: List[str],
    reply_to: Optional[str],
    cc: Optional[str],
    bcc: Optional[str],
) -> None:
    for email in recipients:
        if _stop_event.is_set():
            break
        try:
            _send_via_resend(email, subject, html, inline_images, reply_to, cc, bcc)
            _update_db(campaign_id, sent_delta=1)
        except Exception as exc:
            logger.warning("Resend failed to %s: %s", email, exc)
            _update_db(campaign_id, failed_delta=1)
            _log_failure(campaign_id, email, str(exc))

        time.sleep(settings.smtp_delay_seconds)

    final_status = "stopped" if _stop_event.is_set() else "completed"
    _update_db(campaign_id, status=final_status)


# ── SMTP campaign loop ────────────────────────────────────────────────────────

def _run_smtp_campaign(
    campaign_id: int,
    subject: str,
    html: str,
    inline_images: List[Tuple[str, bytes, str]],
    recipients: List[str],
    reply_to: Optional[str],
    cc: Optional[str],
    bcc: Optional[str],
) -> None:
    def reconnect():
        if settings.smtp_port == 465:
            s = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
        else:
            s = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            s.starttls()
        s.login(settings.smtp_user, settings.smtp_password)
        return s

    # Pre-build MIME template once — base64 encoding happens here
    _TO_PLACEHOLDER = "__RCPT_PLACEHOLDER__"
    tmpl = MIMEMultipart("related") if inline_images else MIMEMultipart("alternative")
    tmpl["Subject"] = subject
    tmpl["From"] = settings.smtp_user
    tmpl["To"] = _TO_PLACEHOLDER
    if reply_to:
        tmpl["Reply-To"] = reply_to
    if cc:
        tmpl["Cc"] = cc
    if bcc:
        tmpl["Bcc"] = bcc
    tmpl.attach(MIMEText(html, "html"))
    for cid, img_bytes, mime_type in inline_images:
        sub = mime_type.split("/", 1)[1] if "/" in mime_type else "jpeg"
        img_part = MIMEImage(img_bytes, _subtype=sub)
        img_part.add_header("Content-ID", f"<{cid}>")
        img_part.add_header("Content-Disposition", "inline")
        tmpl.attach(img_part)

    template_str = tmpl.as_string()
    _to_line_old = f"To: {_TO_PLACEHOLDER}\n"
    cc_extras  = [a.strip() for a in cc.split(",")  if a.strip()] if cc  else []
    bcc_extras = [a.strip() for a in bcc.split(",") if a.strip()] if bcc else []

    smtp = reconnect()
    connect_count = 0
    try:
        for email in recipients:
            if _stop_event.is_set():
                break

            if connect_count > 0 and connect_count % 100 == 0:
                try:
                    smtp.quit()
                except Exception:
                    pass
                smtp = reconnect()

            try:
                msg_str = template_str.replace(_to_line_old, f"To: {email}\n", 1)
                smtp.sendmail(settings.smtp_user, [email] + cc_extras + bcc_extras, msg_str)
                connect_count += 1
                _update_db(campaign_id, sent_delta=1)
            except Exception as exc:
                logger.warning("SMTP failed to %s: %s", email, exc)
                _update_db(campaign_id, failed_delta=1)
                _log_failure(campaign_id, email, str(exc))

            time.sleep(settings.smtp_delay_seconds)

        final_status = "stopped" if _stop_event.is_set() else "completed"
        _update_db(campaign_id, status=final_status)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


# ── Public API ────────────────────────────────────────────────────────────────

def start_campaign(
    campaign_id: int,
    subject: str,
    body_html: str,
    recipients: List[str],
    reply_to: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
):
    global _campaign_thread
    if is_running():
        raise RuntimeError("A campaign is already running")
    _campaign_thread = threading.Thread(
        target=_send_worker,
        args=(campaign_id, subject, body_html, recipients, reply_to, cc, bcc),
        daemon=True,
        name=f"email-campaign-{campaign_id}",
    )
    _campaign_thread.start()
