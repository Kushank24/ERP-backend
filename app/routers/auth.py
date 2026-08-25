from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..permissions import modules_for_username
from ..security import create_access_token, decode_supabase_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


class LoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


@router.post("/login")
def login(body: LoginBody, db: Session = Depends(get_db)):
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
        raise HTTPException(status_code=401, detail="Invalid username or password")

    allowed = modules_for_username(row["username"], row["role"])
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


@router.get("/debug")
def debug_jwt(request: Request):
    """
    Diagnostic endpoint — does NOT require a valid user.

    Call without a token to check configuration, or supply an
    ``Authorization: Bearer <token>`` header to test JWT verification.

    Remove or restrict this endpoint before going to production.
    """
    from ..security import _get_jwks  # local import to avoid circular deps

    # --- configuration summary -------------------------------------------
    jwks_url = settings.supabase_jwks_url
    legacy_configured = bool(settings.supabase_jwt_secret)

    jwks_key_count: Optional[int] = None
    jwks_error: Optional[str] = None
    if jwks_url:
        try:
            jwks_data = _get_jwks()
            jwks_key_count = len(jwks_data.get("keys", []))
        except Exception as exc:
            jwks_error = str(exc)

    result: dict = {
        # ES256 / JWKS path
        "supabase_url_configured": bool(settings.supabase_url),
        "jwks_url": jwks_url,
        "jwks_keys_loaded": jwks_key_count,
        "jwks_error": jwks_error,
        # HS256 / legacy path
        "legacy_jwt_secret_configured": legacy_configured,
        "legacy_jwt_secret_length": len(settings.supabase_jwt_secret) if legacy_configured else 0,
        # token test
        "token_provided": False,
        "token_header_alg": None,
        "token_header_kid": None,
        "token_decode_result": None,
        "token_error": None,
        # hints
        "hint": (
            "Set SUPABASE_URL=https://<ref>.supabase.co in backend/.env so the "
            "backend can verify ES256 access tokens via the JWKS endpoint.  "
            "SUPABASE_JWT_SECRET (legacy HS256) is used as a fallback."
            if not settings.supabase_url
            else None
        ),
    }

    auth_header: Optional[str] = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        from jose import jwt as _jwt
        from jose.exceptions import JWTError as _JWTError

        token = auth_header[7:].strip()
        result["token_provided"] = True

        # Surface the token header so callers can see which algorithm Supabase used.
        try:
            hdr = _jwt.get_unverified_header(token)
            result["token_header_alg"] = hdr.get("alg")
            result["token_header_kid"] = hdr.get("kid")
        except _JWTError:
            result["token_error"] = "Could not parse token header — is this a valid JWT?"
            return result

        if not jwks_url and not legacy_configured:
            result["token_error"] = (
                "Neither SUPABASE_URL nor SUPABASE_JWT_SECRET is set in "
                "backend/.env — cannot verify any Supabase JWT."
            )
            return result

        try:
            decoded = decode_supabase_access_token(token)
            if decoded is not None:
                result["token_decode_result"] = {
                    "ok": True,
                    "sub": decoded.get("sub"),
                    "email": decoded.get("email"),
                    "aud": decoded.get("aud"),
                    "role": decoded.get("role"),
                    "user_metadata_role": (decoded.get("user_metadata") or {}).get("role"),
                }
            else:
                alg = result["token_header_alg"] or "unknown"
                if alg in ("ES256", "ES384", "ES512", "RS256", "RS384", "RS512"):
                    result["token_error"] = (
                        f"Token uses {alg} but could not be verified via JWKS. "
                        "Make sure SUPABASE_URL is set in backend/.env and the "
                        "JWKS endpoint is reachable."
                    )
                else:
                    result["token_error"] = (
                        f"Token uses {alg} and could not be verified. "
                        "Check that SUPABASE_JWT_SECRET in backend/.env matches "
                        "the Legacy JWT Secret shown in Supabase → Project Settings "
                        "→ API → JWT Settings, then restart uvicorn."
                    )
        except Exception as exc:  # pragma: no cover
            result["token_error"] = f"Unexpected decode error: {exc}"

    return result
