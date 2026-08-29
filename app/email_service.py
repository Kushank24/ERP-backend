"""Background SMTP email sender for bulk campaigns."""
from __future__ import annotations

import logging
import mimetypes
import re
import smtplib
import threading
import time
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional, Tuple

from .config import settings
from .db import SessionLocal

# Must match the upload dir used by email_campaigns.py
_IMG_DIR = Path(__file__).resolve().parent.parent / "uploaded_images"

logger = logging.getLogger(__name__)

# ── CID image embedding ───────────────────────────────────────────────────────

_IMG_SRC_RE = re.compile(
    r'src="(https?://[^"]+/email-campaigns/images/([^"?#]+))"',
    re.IGNORECASE,
)


def _embed_images(html: str) -> Tuple[str, List[Tuple[str, bytes, str]], List[Path]]:
    """
    Replace <img src="http[s]://…/email-campaigns/images/{name}"> with
    src="cid:{cid}" and return (modified_html, [(cid, bytes, mime_type)], [paths_loaded]).
    Images that cannot be read from disk are left as-is.
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

_campaign_thread: threading.Thread | None = None
_stop_event = threading.Event()


def is_running() -> bool:
    return _campaign_thread is not None and _campaign_thread.is_alive()


def stop_campaign():
    _stop_event.set()


def _update_db(campaign_id: int, sent_delta: int = 0, failed_delta: int = 0, status: str | None = None):
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
            db.execute(text(f"UPDATE email_campaigns SET {', '.join(parts)} WHERE id = :id"), params)
            db.commit()
    except Exception as exc:
        logger.error("Campaign DB update failed: %s", exc)


def _send_worker(
    campaign_id: int,
    subject: str,
    body_html: str,
    recipients: List[str],
    reply_to: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
):
    from sqlalchemy import text

    _stop_event.clear()

    def reconnect():
        s = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        s.starttls()
        s.login(settings.smtp_user, settings.smtp_password)
        return s

    smtp = None
    try:
        smtp = reconnect()
        connect_count = 0

        # ── Build & serialise the message template ONCE ───────────────────────
        # Images are base64-encoded here — this is the expensive step.
        # Per-recipient we only swap the To: header in the pre-built string.
        _TO_PLACEHOLDER = "__RCPT_PLACEHOLDER__"
        html_to_send, inline_images, image_paths = _embed_images(body_html)

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
        tmpl.attach(MIMEText(html_to_send if inline_images else body_html, "html"))
        for cid, img_bytes, mime_type in inline_images:
            sub = mime_type.split("/", 1)[1] if "/" in mime_type else "jpeg"
            img_part = MIMEImage(img_bytes, _subtype=sub)
            img_part.add_header("Content-ID", f"<{cid}>")
            img_part.add_header("Content-Disposition", "inline")
            tmpl.attach(img_part)

        template_str = tmpl.as_string()           # serialised once — images fully encoded
        _to_line_old = f"To: {_TO_PLACEHOLDER}\n"

        # Delete source image files — they are now embedded in template_str
        for p in image_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("Could not delete image file %s: %s", p, exc)
        cc_extras  = [a.strip() for a in cc.split(",")  if a.strip()] if cc  else []
        bcc_extras = [a.strip() for a in bcc.split(",") if a.strip()] if bcc else []
        # ─────────────────────────────────────────────────────────────────────

        for email in recipients:
            if _stop_event.is_set():
                break

            # Reconnect every 100 emails to avoid server-side idle timeout
            if connect_count > 0 and connect_count % 100 == 0:
                try:
                    smtp.quit()
                except Exception:
                    pass
                smtp = reconnect()

            try:
                # Swap only the To: line — no re-encoding of images
                msg_str = template_str.replace(_to_line_old, f"To: {email}\n", 1)
                all_rcpts = [email] + cc_extras + bcc_extras
                smtp.sendmail(settings.smtp_user, all_rcpts, msg_str)
                connect_count += 1
                _update_db(campaign_id, sent_delta=1)
            except Exception as exc:
                logger.warning("Failed to send to %s: %s", email, exc)
                _update_db(campaign_id, failed_delta=1)
                try:
                    with SessionLocal() as db:
                        db.execute(
                            text(
                                "INSERT INTO email_campaign_failures (campaign_id, email, error) "
                                "VALUES (:cid, :em, :err)"
                            ),
                            {"cid": campaign_id, "em": email, "err": str(exc)[:500]},
                        )
                        db.commit()
                except Exception:
                    pass

            time.sleep(settings.smtp_delay_seconds)

        final_status = "stopped" if _stop_event.is_set() else "completed"
        _update_db(campaign_id, status=final_status)

    except Exception as exc:
        logger.error("Campaign %d worker crashed: %s", campaign_id, exc)
        _update_db(campaign_id, status="failed")
    finally:
        if smtp:
            try:
                smtp.quit()
            except Exception:
                pass


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
