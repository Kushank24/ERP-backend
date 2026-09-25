"""
Cloudinary integration for sales-order documents (invoice, e-way bill, LR copy).

Uploaded with type="authenticated" — Cloudinary's stronger access-control
tier, chosen because these are GST invoices and e-way bills (compliance-
sensitive financial documents). Unlike the public "upload" delivery type used
elsewhere in this app for campaign images, an authenticated asset cannot be
fetched with a plain URL at all — every request must carry a valid signature,
and that signature expires.

The practical consequence: nothing permanent is stored. What's persisted in
the database is the Cloudinary identity of the file — public_id,
resource_type, format — and a fresh signed URL is generated on demand, each
time someone actually wants to view it (see get_signed_url). Never cache or
persist a signed URL; by design it stops working after SIGNED_URL_TTL_SECONDS.

Setup required in the Cloudinary dashboard before this works at all:
  Settings → Security → "Allow delivery of PDF and ZIP files"
Free-tier accounts block PDF *delivery* by default (separate from upload) as
an anti-abuse measure — uploads will succeed and then every view will 401
until this is enabled.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import cloudinary
import cloudinary.uploader
import cloudinary.utils

from .config import settings

logger = logging.getLogger(__name__)

#: How long a generated view URL stays valid. Short on purpose — it is
#: generated fresh on every view request, so there is no benefit to a long
#: TTL and real cost to one (a leaked link stays live longer).
SIGNED_URL_TTL_SECONDS = 5 * 60

_configured = False


def _ensure_configured() -> None:
    """Apply cloudinary.config() once, lazily, from settings."""
    global _configured
    if _configured:
        return
    if not (settings.cloudinary_cloud_name and settings.cloudinary_api_key and settings.cloudinary_api_secret):
        raise RuntimeError(
            "Cloudinary is not configured — set CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET."
        )
    cloudinary.config(
        cloud_name=settings.cloudinary_cloud_name,
        api_key=settings.cloudinary_api_key,
        api_secret=settings.cloudinary_api_secret,
        secure=True,
    )
    _configured = True


def upload_document(raw: bytes, filename: str, folder: str) -> dict:
    """
    Upload raw bytes to Cloudinary under authenticated delivery.

    Returns the minimal identity needed to later generate a signed view URL:
    {"public_id", "resource_type", "format", "original_filename", "bytes"}.
    This dict is what gets stored in sales_orders.invoice_document /
    eway_bill_document — never a URL, since an authenticated asset has no
    permanent one.

    resource_type="auto" rather than hardcoding "image": invoices/e-way
    bills are usually PDFs, but a photographed paper e-way bill (jpg/png) is
    a realistic case too, and "auto" classifies either correctly.
    """
    _ensure_configured()
    import io

    buf = io.BytesIO(raw)
    buf.name = filename  # Cloudinary's SDK reads this to infer format/extension

    try:
        result = cloudinary.uploader.upload(
            buf,
            type="authenticated",
            resource_type="auto",
            folder=folder,
            use_filename=True,
            unique_filename=True,
        )
    except Exception as exc:
        logger.error("Cloudinary upload failed for %r: %s", filename, exc)
        raise

    return {
        "public_id": result["public_id"],
        "resource_type": result["resource_type"],
        "format": result.get("format", ""),
        "original_filename": filename,
        "bytes": result.get("bytes"),
    }


#: Max width/height for a raster document scan. Deliberately far more
#: generous than the 800px used for marketing email images elsewhere in this
#: app (email_campaigns._compress_image) — that number is tuned for a
#: thumbnail meant to look decent in an inbox; this one has to keep printed
#: text legible on a GST invoice or e-way bill, which is a different job.
_MAX_DOCUMENT_PX = 2200
_JPEG_QUALITY = 88


def optimize_if_image(raw: bytes, filename: str) -> bytes:
    """
    Downscale and re-compress raw bytes if — and only if — they are a raster
    image (jpg/png/webp/etc.). PDFs pass through completely untouched.

    This is not a filename check. Pillow is asked to open the bytes
    directly; a PDF makes it raise UnidentifiedImageError immediately
    (Pillow has no PDF-reading codec at all — it can only write PDFs, not
    read them), which is exactly the signal used here to skip PDFs rather
    than risk mis-detecting one from an untrustworthy extension.

    PDF compression was deliberately NOT implemented after checking what is
    actually possible: an upload-time `quality` parameter to Cloudinary has
    no effect on the stored PDF at all (verified empirically — two uploads
    of the same 5-line-per-page, 5-page test PDF, one with
    quality="auto:eco" and one without, both came back as exactly 5577
    bytes), and the delivery-time q_auto transformation is not reachable
    through private_download_url either (verified empirically — passing
    quality/fetch_format/transformation kwargs produced byte-for-byte
    identical URLs; the endpoint that method calls is a dedicated
    force-download API, not the transformable CDN delivery pipeline).
    Getting real PDF compression would mean re-encoding the PDF's internal
    content streams with a dedicated PDF library, which risks the one thing
    that must not happen to a GST invoice or e-way bill: text becoming
    blurry or non-searchable. Not worth that risk for a file type whose
    honest text content is typically only a few KB anyway (as the same test
    showed).

    Never returns something larger than the input: if resizing/re-encoding
    doesn't actually save space (already small, or an unusual format Pillow
    round-trips inefficiently), the original bytes are kept.
    """
    from PIL import Image
    import io as _io

    try:
        img = Image.open(_io.BytesIO(raw))
        img.load()  # force full decode now — confirms it's a real, complete image
    except Exception:
        return raw  # not an image Pillow can read (PDF, or corrupt) — leave untouched

    try:
        img = img.convert("RGB")
        w, h = img.size
        if w > _MAX_DOCUMENT_PX or h > _MAX_DOCUMENT_PX:
            ratio = _MAX_DOCUMENT_PX / max(w, h)
            img = img.resize((round(w * ratio), round(h * ratio)), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        optimized = buf.getvalue()
    except Exception as exc:
        logger.warning("Image optimization failed for %r, uploading original: %s", filename, exc)
        return raw

    if len(optimized) >= len(raw):
        return raw
    logger.info(
        "Optimized %r: %d -> %d bytes (%.0f%% smaller)",
        filename, len(raw), len(optimized), 100 * (1 - len(optimized) / len(raw)),
    )
    return optimized


def get_signed_url(document: dict, expires_in_seconds: int = SIGNED_URL_TTL_SECONDS) -> str:
    """
    Generate a fresh, time-limited signed URL for a previously-uploaded
    authenticated document. Call this every time a view is requested — the
    result must never be cached or stored.
    """
    _ensure_configured()
    expires_at = int(time.time()) + expires_in_seconds
    return cloudinary.utils.private_download_url(
        document["public_id"],
        document.get("format") or "",
        resource_type=document.get("resource_type", "image"),
        type="authenticated",
        expires_at=expires_at,
    )
