"""
Upload-endpoint hardening found during a security review.

Two independent bugs, both real and reachable by any authenticated user with
the email_campaigns module:

  1. No file-upload endpoint had a size limit — `await file.read()` buffered
     the entire request body into memory unconditionally. A large enough
     POST is a plain memory-exhaustion DoS.

  2. The image-upload endpoint's content check was extension-only. GIFs
     skipped Pillow validation entirely ("kept as-is" for animation), and a
     Pillow decode failure on any other extension silently fell back to
     storing the raw, unvalidated bytes rather than rejecting the upload.
     Renaming any file to "x.gif" bypassed every content check and got it
     stored + served at a public, unauthenticated URL.

These tests exercise real bytes through the real functions — not mocks —
because this is exactly the class of bug a mock would hide.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app.upload_limits import MAX_UPLOAD_BYTES, read_limited


def _upload_file(content: bytes, filename: str = "test.bin") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": "application/octet-stream"}),
    )


# ---------------------------------------------------------------------------
# 1. Size limit — real bytes, real UploadFile, no mocking
# ---------------------------------------------------------------------------


def test_upload_under_the_limit_is_returned_unchanged():
    content = b"x" * 1000
    result = asyncio.run(read_limited(_upload_file(content)))
    assert result == content


def test_upload_exactly_at_the_limit_is_accepted():
    content = b"x" * MAX_UPLOAD_BYTES
    result = asyncio.run(read_limited(_upload_file(content)))
    assert len(result) == MAX_UPLOAD_BYTES


def test_upload_one_byte_over_the_limit_is_rejected_with_413():
    content = b"x" * (MAX_UPLOAD_BYTES + 1)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(read_limited(_upload_file(content)))
    assert exc_info.value.status_code == 413


def test_custom_max_bytes_is_honoured():
    small_limit = 100
    content = b"x" * 101
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(read_limited(_upload_file(content), max_bytes=small_limit))
    assert exc_info.value.status_code == 413


# ---------------------------------------------------------------------------
# 2. Image content validation — the extension-bypass bug
# ---------------------------------------------------------------------------


def _make_real_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
    return buf.getvalue()


def _make_real_gif() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), color="blue").save(buf, format="GIF")
    return buf.getvalue()


def test_verify_accepts_a_genuine_png():
    from app.routers.email_campaigns import _verify_is_image
    _verify_is_image(_make_real_png())  # must not raise


def test_verify_accepts_a_genuine_gif():
    from app.routers.email_campaigns import _verify_is_image
    _verify_is_image(_make_real_gif())  # must not raise


def test_verify_rejects_non_image_bytes_named_like_an_image():
    """
    This is the exact bypass: a plain text/script payload, the kind of
    content that used to sail through when named "x.gif". Must now raise a
    clean 400, not silently pass through and not 500.
    """
    from app.routers.email_campaigns import _verify_is_image
    payload = b"<script>alert(document.cookie)</script>"
    with pytest.raises(HTTPException) as exc_info:
        _verify_is_image(payload)
    assert exc_info.value.status_code == 400


def test_verify_rejects_empty_content():
    from app.routers.email_campaigns import _verify_is_image
    with pytest.raises(HTTPException):
        _verify_is_image(b"")


def test_verify_rejects_truncated_image_data():
    """A file that starts like a PNG but is cut off mid-stream."""
    from app.routers.email_campaigns import _verify_is_image
    real_png = _make_real_png()
    truncated = real_png[: len(real_png) // 2]
    with pytest.raises(HTTPException) as exc_info:
        _verify_is_image(truncated)
    assert exc_info.value.status_code == 400


def test_compress_image_rejects_non_image_bytes_instead_of_returning_them_raw():
    """
    The original bug: `except Exception: return raw, ext` silently wrote the
    unvalidated original bytes to disk. It must now raise instead.
    """
    from app.routers.email_campaigns import _compress_image
    payload = b"not an image at all"
    with pytest.raises(HTTPException) as exc_info:
        _compress_image(payload, ".jpg")
    assert exc_info.value.status_code == 400


def test_compress_image_actually_compresses_a_real_png():
    from app.routers.email_campaigns import _compress_image
    out_bytes, out_ext = _compress_image(_make_real_png(), ".png")
    assert out_ext == ".jpg"
    assert out_bytes[:2] == b"\xff\xd8"  # JPEG magic bytes


def test_gif_upload_path_now_validates_before_storing():
    """
    Regression test for the specific bypass: GIFs were exempted from
    _compress_image (to preserve animation) and that exemption used to mean
    "exempted from all validation". The upload endpoint must now call
    _verify_is_image unconditionally before branching on extension.
    """
    import inspect
    from app.routers import email_campaigns

    src = inspect.getsource(email_campaigns.upload_image)
    verify_call_pos = src.find("_verify_is_image(")
    gif_branch_pos = src.find('ext not in (".gif",)')
    assert verify_call_pos != -1, "_verify_is_image must be called in upload_image"
    assert verify_call_pos < gif_branch_pos, (
        "validation must happen before the GIF exemption branch, "
        "or GIFs bypass content validation again"
    )
