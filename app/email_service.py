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
import socket
import threading
import time
import urllib.error
import urllib.request
from email.mime.application import MIMEApplication
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

RESEND_ENDPOINT = "https://api.resend.com/emails"

# Cloudflare sits in front of api.resend.com and answers 403 with its own
# numeric codes, which look like Resend errors but are not — the request never
# reached Resend. Distinguishing the two is the difference between "fix your
# sender domain" and "fix your egress".
_CLOUDFLARE_HINTS = {
    "1010": (
        "Cloudflare blocked the request based on the client signature (error "
        "1010) — it never reached Resend, so this is not an API-key or "
        "sender-domain problem. Usually the User-Agent or the TLS fingerprint "
        "of Python's urllib. Try RESEND_USER_AGENT with a conventional value, "
        "or switch this sender to the official resend SDK / httpx."
    ),
    "1020": (
        "Cloudflare firewall rule denied the request (error 1020). The host's "
        "outbound IP is likely blocked or geo-filtered."
    ),
    "1015": "Cloudflare rate-limited the request (error 1015). Slow the send rate.",
}


def _header(headers: dict, name: str) -> Optional[str]:
    """
    Case-insensitive header lookup.

    HTTP header names are case-insensitive, and what comes back here is a plain
    dict built from the response. Resend/Cloudflare send ``CF-RAY``, so a
    lowercase-only ``.get("cf-ray")`` silently returned None and the Ray ID —
    the one identifier support asks for — was dropped.
    """
    if not headers:
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())


def _describe_resend_failure(status: int, body: str, headers: dict) -> str:
    """
    Build a diagnosis from a failed Resend call.

    Includes the Cloudflare Ray ID when present — that is the first thing
    Cloudflare or Resend support will ask for, and it is invisible in the
    current logs.
    """
    bits = [f"HTTP {status}"]

    cf_code = None
    m = re.search(r"error code:\s*(\d+)", body or "")
    if m:
        cf_code = m.group(1)

    ray = _header(headers, "cf-ray")
    server = _header(headers, "server")
    if ray:
        bits.append(f"cf-ray={ray}")
    if server:
        bits.append(f"server={server}")

    if cf_code:
        bits.append(f"cloudflare={cf_code}")
        hint = _CLOUDFLARE_HINTS.get(
            cf_code, f"Cloudflare denied the request (error {cf_code}) before it reached Resend."
        )
    elif status == 401:
        hint = (
            "Resend rejected the API key. Check RESEND_API_KEY is set in the "
            "hosting environment (not only in local .env) and has not been revoked."
        )
    elif status == 403:
        hint = (
            "Resend refused the request. The most common cause is an unverified "
            "sender domain — the 'from' address domain must be verified in the "
            "Resend dashboard."
        )
    elif status == 422:
        hint = "Resend rejected the payload — check the 'from' address and recipient format."
    elif status == 429:
        hint = "Resend rate limit hit. Increase SMTP_DELAY_SECONDS."
    else:
        hint = "Unexpected response from Resend."

    snippet = (body or "").strip().replace("\n", " ")[:300]
    return f"{' '.join(bits)}: {snippet} | {hint}"


def _send_via_resend(
    to_email: str,
    subject: str,
    html: str,
    inline_images: List[Tuple[str, bytes, str]],
    reply_to: Optional[str],
    cc: Optional[str],
    bcc: Optional[str],
    attachments: Optional[List[Tuple[str, bytes, str]]] = None,
) -> None:
    """
    inline_images: (content_id, bytes, mime_type) — embedded via cid: in the
        HTML body (used by the bulk campaign editor's inline images).
    attachments: (filename, bytes, mime_type) — a normal file attachment with
        no content_id, so mail clients show it as a downloadable file rather
        than rendering it inline (used to attach the offer PDF to a reminder).
    """
    from_email = settings.resend_from_email or settings.smtp_user
    payload: dict = {
        "from": from_email,
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

    all_attachments = [
        {
            "content_id": cid,
            "filename": f"{cid}.jpg",
            "content": base64.b64encode(img_bytes).decode(),
        }
        for cid, img_bytes, _ in inline_images
    ] + [
        {
            "filename": filename,
            "content": base64.b64encode(file_bytes).decode(),
        }
        for filename, file_bytes, _ in (attachments or [])
    ]
    if all_attachments:
        payload["attachments"] = all_attachments

    body_bytes = json.dumps(payload).encode()
    req = urllib.request.Request(
        RESEND_ENDPOINT,
        data=body_bytes,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # A conventional User-Agent. The previous value ("esafe-erp/1.0")
            # coincided with Cloudflare 1010 blocks in production; Cloudflare
            # scores unusual agents as automated clients. Override with
            # RESEND_USER_AGENT if this needs tuning without a deploy.
            "User-Agent": settings.resend_user_agent,
        },
        method="POST",
    )

    logger.debug(
        "Resend POST %s from=%r to=%r attachments=%d payload_bytes=%d ua=%r",
        RESEND_ENDPOINT, from_email, to_email,
        len(payload.get("attachments") or []), len(body_bytes),
        settings.resend_user_agent,
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if resp.status not in (200, 201):
                detail = _describe_resend_failure(resp.status, raw, dict(resp.headers))
                logger.error("Resend rejected message to %s — %s", to_email, detail)
                raise RuntimeError(detail)
            logger.debug("Resend accepted message to %s: %s", to_email, raw[:200])

    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - body already consumed
            pass
        detail = _describe_resend_failure(exc.code, raw, dict(exc.headers or {}))
        logger.error(
            "Resend rejected message to %s (from=%r) — %s", to_email, from_email, detail
        )
        raise RuntimeError(detail) from exc

    except urllib.error.URLError as exc:
        # DNS failure, TLS failure, no route, connection refused. Previously
        # this surfaced only as "<urlopen error ...>" with no context — and
        # campaign 10 failed exactly here with "[Errno 101] Network is
        # unreachable", which says nothing about what was being reached.
        detail = (
            f"Could not reach {RESEND_ENDPOINT}: {exc.reason!r}. "
            "The host cannot open an outbound HTTPS connection to Resend — "
            "check egress rules, DNS, and whether the platform requires IPv4 "
            "(Errno 101 'Network is unreachable' usually means an IPv6 route "
            "was attempted with no IPv6 connectivity)."
        )
        logger.error("Resend unreachable for %s — %s", to_email, detail)
        raise RuntimeError(detail) from exc

    except socket.timeout as exc:
        detail = f"Resend did not respond within 30s ({RESEND_ENDPOINT})."
        logger.error("Resend timeout for %s — %s", to_email, detail)
        raise RuntimeError(detail) from exc


# ── Single transactional email ────────────────────────────────────────────────
# Used by scheduled jobs (e.g. app/jobs/offer_reminders.py) that send one email
# per recipient outside the bulk-campaign flow — no campaign_id, no progress
# tracking in email_campaigns, no CID image embedding. Respects the same
# EMAIL_PROVIDER selection and the same Resend error classification as the
# campaign path, so a job failure is diagnosable the same way.

def _send_single_via_smtp(
    to_email: str,
    subject: str,
    html: str,
    attachments: Optional[List[Tuple[str, bytes, str]]] = None,
    bcc: Optional[str] = None,
) -> None:
    msg = MIMEMultipart("mixed") if attachments else MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = to_email
    # Bcc is deliberately not set as a header — a Bcc header would defeat its
    # own purpose by revealing the address to the primary recipient. It is
    # only added to the SMTP envelope recipient list below.
    msg.attach(MIMEText(html, "html"))

    for filename, file_bytes, mime_type in attachments or []:
        maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
        part = MIMEApplication(file_bytes, _subtype=subtype or "octet-stream")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    envelope_recipients = [to_email] + ([bcc] if bcc else [])

    if settings.smtp_port == 465:
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        smtp.starttls()
    try:
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.sendmail(settings.smtp_user, envelope_recipients, msg.as_string())
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def send_transactional_email(
    to_email: str,
    subject: str,
    html: str,
    attachments: Optional[List[Tuple[str, bytes, str]]] = None,
    bcc: Optional[str] = None,
) -> None:
    """
    Send one email now, using whichever provider EMAIL_PROVIDER selects
    (same auto/resend/smtp logic as bulk campaigns). Raises on failure — the
    caller decides what "failed" means for that job (skip, retry, log).

    attachments: (filename, bytes, mime_type) — plain file attachments (e.g.
    the offer PDF), distinct from the campaign editor's inline cid: images.
    bcc: a single address to blind-copy (e.g. an internal record-keeping
    inbox). Not visible to to_email in either provider path.
    """
    provider = settings.email_provider.lower()
    if provider == "resend":
        use_resend = True
    elif provider == "smtp":
        use_resend = False
    else:  # auto
        use_resend = bool(settings.resend_api_key)

    if use_resend and not settings.resend_api_key:
        logger.error("EMAIL_PROVIDER=resend but RESEND_API_KEY is not set; falling back to SMTP")
        use_resend = False

    if use_resend:
        _send_via_resend(to_email, subject, html, [], None, None, bcc, attachments)
    else:
        _send_single_via_smtp(to_email, subject, html, attachments, bcc)


def resend_diagnostics(probe: bool = True) -> dict:
    """
    Report how email sending is configured, and optionally make one live call
    to Resend to see what actually happens.

    Built because the failure was invisible: the reason was being written to
    ``email_campaign_failures.error`` but nothing surfaced it, and the app had
    no logging configuration so INFO lines never appeared either. Hitting this
    from the hosting environment answers the question in one request.

    Never returns the API key — only whether it is present, its length, and
    its prefix, which is enough to tell "not set in this environment" from
    "set but wrong".
    """
    key = settings.resend_api_key or ""
    from_email = settings.resend_from_email or settings.smtp_user or ""
    provider = (settings.email_provider or "auto").lower()

    if provider == "resend":
        chosen = "resend"
    elif provider == "smtp":
        chosen = "smtp"
    else:
        chosen = "resend" if key else "smtp"

    result: dict = {
        "email_provider_setting": provider,
        "provider_that_would_be_used": chosen,
        "resend_api_key_present": bool(key),
        "resend_api_key_length": len(key),
        "resend_api_key_prefix": (key[:6] + "…") if key else None,
        "resend_api_key_format_looks_valid": key.startswith("re_") and len(key) > 20,
        "from_address": from_email or None,
        "from_address_source": (
            "RESEND_FROM_EMAIL" if settings.resend_from_email
            else ("SMTP_USER (fallback)" if settings.smtp_user else "unset")
        ),
        "from_domain": from_email.split("@")[-1] if "@" in from_email else None,
        "user_agent": settings.resend_user_agent,
        "endpoint": RESEND_ENDPOINT,
        "smtp_host": settings.smtp_host,
        "smtp_user_present": bool(settings.smtp_user),
        "delay_seconds": settings.smtp_delay_seconds,
        "probe": None,
    }

    warnings: List[str] = []
    if chosen == "resend" and not key:
        warnings.append("EMAIL_PROVIDER=resend but RESEND_API_KEY is empty in this environment.")
    if key and not result["resend_api_key_format_looks_valid"]:
        warnings.append("RESEND_API_KEY does not look like a Resend key (expected 're_' prefix).")
    if not from_email:
        warnings.append("No from address: set RESEND_FROM_EMAIL (or SMTP_USER).")
    if result["from_domain"] in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com"}:
        warnings.append(
            f"From domain '{result['from_domain']}' is a public mailbox provider and cannot "
            "be verified in Resend. Use an address on a domain you own and have verified."
        )
    result["warnings"] = warnings

    if not probe:
        return result

    # Live probe: DNS + TCP + TLS + one authenticated request. Deliberately
    # posts an invalid payload (empty recipient list) so nothing is delivered —
    # we only care whether we get *a Resend answer* rather than a Cloudflare
    # block or a network error.
    probe_result: dict = {"stage": "dns"}
    try:
        infos = socket.getaddrinfo("api.resend.com", 443, proto=socket.IPPROTO_TCP)
        probe_result["resolved_addresses"] = sorted(
            {i[4][0] for i in infos}
        )[:6]
        probe_result["has_ipv4"] = any(i[0] == socket.AF_INET for i in infos)
        probe_result["has_ipv6"] = any(i[0] == socket.AF_INET6 for i in infos)
    except Exception as exc:
        probe_result["stage"] = "dns_failed"
        probe_result["error"] = repr(exc)
        result["probe"] = probe_result
        return result

    probe_result["stage"] = "http"
    req = urllib.request.Request(
        RESEND_ENDPOINT,
        data=json.dumps({"from": from_email, "to": [], "subject": "", "html": ""}).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": settings.resend_user_agent,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            probe_result.update(
                stage="reached_resend",
                status=resp.status,
                cf_ray=_header(dict(resp.headers), "cf-ray"),
                server=_header(dict(resp.headers), "server"),
                body=resp.read().decode("utf-8", "replace")[:300],
                verdict="Reached Resend and it answered.",
            )
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        headers = dict(exc.headers or {})
        cf_code = re.search(r"error code:\s*(\d+)", raw or "")
        blocked = bool(cf_code)
        probe_result.update(
            stage="cloudflare_blocked" if blocked else "reached_resend",
            status=exc.code,
            cf_ray=_header(headers, "cf-ray"),
            server=_header(headers, "server"),
            cloudflare_error=cf_code.group(1) if cf_code else None,
            body=(raw or "").strip().replace("\n", " ")[:300],
            verdict=_describe_resend_failure(exc.code, raw, headers),
        )
        # 401/422 from Resend itself still proves connectivity is fine.
        if not blocked and exc.code in (401, 422, 400):
            probe_result["connectivity"] = "ok — Resend answered, so DNS/TLS/egress all work"
    except urllib.error.URLError as exc:
        probe_result.update(
            stage="network_unreachable",
            error=repr(exc.reason),
            verdict=(
                "Could not open an outbound HTTPS connection to api.resend.com. "
                "Check egress/firewall rules and IPv6 availability."
            ),
        )
    except socket.timeout:
        probe_result.update(stage="timeout", verdict="No response within 15s.")

    result["probe"] = probe_result
    return result


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

    provider = settings.email_provider.lower()
    if provider == "resend":
        use_resend = True
    elif provider == "smtp":
        use_resend = False
    else:  # auto
        use_resend = bool(settings.resend_api_key)

    if use_resend and not settings.resend_api_key:
        logger.error("EMAIL_PROVIDER=resend but RESEND_API_KEY is not set; falling back to SMTP")
        use_resend = False

    logger.info("Campaign %d: using %s", campaign_id, "Resend" if use_resend else "SMTP")

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
