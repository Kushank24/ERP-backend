from __future__ import annotations

import logging
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..permissions import modules_for_role
from ..security import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------
# /auth/login is one of only two endpoints reachable without a token, which
# makes it the brute-force target. We keep a short sliding window of failed
# attempts per client IP and per username, and start refusing once either
# exceeds the threshold.
#
# Deliberate limitations, documented rather than hidden:
#   * State is per-process. Behind multiple uvicorn workers or replicas the
#     effective limit is MAX_FAILURES x worker count. It raises the cost of a
#     brute-force attempt substantially but is not a distributed rate limiter —
#     move to Redis if the API is ever scaled out horizontally.
#   * Keyed on the direct client address. Behind a proxy that is the proxy's
#     IP unless the ASGI server is configured to honour X-Forwarded-For.
# ---------------------------------------------------------------------------

_WINDOW_SECONDS = 15 * 60
_MAX_FAILURES = 8
_MAX_TRACKED_KEYS = 10_000

_failures: dict[str, deque[float]] = {}


def _prune(key: str, now: float) -> deque[float]:
    hits = _failures.get(key)
    if hits is None:
        hits = deque()
        # Bound memory: if the table is saturated (likely a distributed
        # attack), drop the oldest-touched entries rather than grow forever.
        if len(_failures) >= _MAX_TRACKED_KEYS:
            for stale in list(_failures)[: _MAX_TRACKED_KEYS // 10]:
                _failures.pop(stale, None)
        _failures[key] = hits
    cutoff = now - _WINDOW_SECONDS
    while hits and hits[0] < cutoff:
        hits.popleft()
    return hits


def _check_not_throttled(keys: list[str]) -> None:
    now = time.time()
    for key in keys:
        if len(_prune(key, now)) >= _MAX_FAILURES:
            raise HTTPException(
                status_code=429,
                detail="Too many failed sign-in attempts. Try again later.",
                headers={"Retry-After": str(_WINDOW_SECONDS)},
            )


def _record_failure(keys: list[str]) -> None:
    now = time.time()
    for key in keys:
        _prune(key, now).append(now)


def _clear(keys: list[str]) -> None:
    for key in keys:
        _failures.pop(key, None)


class LoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


@router.post("/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    throttle_keys = [
        f"ip:{client_ip}",
        f"user:{body.username.strip().lower()}",
    ]
    _check_not_throttled(throttle_keys)
    try:
        row = db.execute(
            text(
                "SELECT id, username, password_hash, role FROM app_users WHERE username = :u"
            ),
            {"u": body.username.strip()},
        ).mappings().first()
    except SQLAlchemyError as exc:
        logger.exception("Database error during login")
        root = getattr(exc, "orig", exc)
        msg = str(root)
        detail = (
            "Cannot connect to the database. Set DATABASE_URL in ERP/backend/.env "
            "(use postgresql+psycopg://… for Supabase; URL-encode @ in the password). "
            "TLS is set automatically for supabase.co / supabase.com hosts."
        )
        if "127.0.0.1" in msg and "54322" in msg:
            detail = (
                "DATABASE_URL is still the default local URL (127.0.0.1:54322). "
                "Add DATABASE_URL=postgresql+psycopg://postgres:PASSWORD@db.YOUR_REF.supabase.co:5432/postgres "
                "to ERP/backend/.env (encode special chars in PASSWORD). Restart uvicorn."
            )
        elif "failed to resolve host" in msg or "nodename nor servname" in msg:
            detail = (
                "DNS could not resolve the database host. Many networks cannot use db.<ref>.supabase.co "
                "(IPv6 / DNS). In Supabase: Settings → Database → Connection string → copy "
                "Transaction pooler or Session pooler (host aws-0-REGION.pooler.supabase.com, "
                "user postgres.YOUR_PROJECT_REF). Set DATABASE_URL with scheme postgresql+psycopg:// "
                "and URL-encode special characters in the password."
            )
        raise HTTPException(status_code=503, detail=detail) from None
    raw_hash = row["password_hash"] if row else None
    if isinstance(raw_hash, memoryview):
        raw_hash = raw_hash.tobytes().decode("utf-8")
    elif isinstance(raw_hash, bytes):
        raw_hash = raw_hash.decode("utf-8")
    elif raw_hash is not None:
        raw_hash = str(raw_hash)

    if not row or not verify_password(body.password, raw_hash or ""):
        _record_failure(throttle_keys)
        logger.warning(
            "Failed sign-in for username=%r from ip=%r",
            body.username.strip(),
            client_ip,
        )
        raise HTTPException(status_code=401, detail="Invalid username or password")

    _clear(throttle_keys)
    allowed = modules_for_role(row["role"])
    token = create_access_token(
        row["username"],
        {"uid": row["id"], "role": row["role"]},
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "allowed_modules": allowed,
        },
    }


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return user
