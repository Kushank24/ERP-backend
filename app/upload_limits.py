"""
Shared upload size guard.

Found during a security review: every file-upload endpoint (campaign image,
contact-list parser, product bulk-import) called ``await file.read()``
unconditionally — no Content-Length check, no bound on the read itself. A
large-enough POST body is read entirely into memory before any validation
runs, which is a plain memory-exhaustion DoS on an authenticated endpoint.

Read with a bound instead of checking size after the fact: ``UploadFile.read``
accepts a byte limit, so this never buffers more than ``max_bytes + 1`` bytes
into memory regardless of how large the actual request body is.
"""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

#: Generous for the current use cases (marketing images, CSV/XLSX contact
#: lists, BOQ product imports) while still bounding worst-case memory use per
#: request. Raise per-call via `max_bytes=` if a specific endpoint needs more.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


async def read_limited(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """
    Read an UploadFile's body, rejecting it with 413 if it exceeds max_bytes.

    Reads at most max_bytes + 1 so the check is exact without ever buffering
    more than one byte past the limit.
    """
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            413,
            f"File too large — max {max_bytes // (1024 * 1024)} MB.",
        )
    return data
