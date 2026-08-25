from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .permissions import modules_for_username
from .security import decode_supabase_access_token, decode_token

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


def _resolve_supabase_role(sup: dict) -> str:
    """
    Determine the application role for a Supabase Auth user.

    Resolution order (first non-empty value wins):
    1. ``app_metadata.role``   — set server-side via Supabase Admin API; cannot
                                  be overwritten by the user themselves.
    2. ``user_metadata.role``  — set by the user or during sign-up.
    3. Admin e-mail list       — SUPABASE_ADMIN_EMAILS env var; any matching
                                  address always gets "admin".
    4. SUPABASE_DEFAULT_ROLE   — configurable fallback (default: "admin").
    """
    # 1. app_metadata.role  (server-controlled — most trusted)
    am = sup.get("app_metadata") or {}
    if not isinstance(am, dict):
        am = {}
    if am.get("role"):
        return str(am["role"]).strip()

    # 2. user_metadata.role
    um = sup.get("user_metadata") or {}
    if not isinstance(um, dict):
        um = {}
    if um.get("role"):
        return str(um["role"]).strip()

    # 3. Admin e-mail list
    email = (sup.get("email") or "").strip().lower()
    if email and email in settings.supabase_admin_email_set:
        return "admin"

    # 4. Configurable default
    return settings.supabase_default_role or "admin"


def get_current_user(
    creds: Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    token = creds.credentials

    # ------------------------------------------------------------------
    # Supabase Auth path  (ES256 via JWKS  or  HS256 via legacy secret)
    # ------------------------------------------------------------------
    # We attempt Supabase verification whenever *either* key source is
    # configured — the new JWKS path does not need the legacy secret.
    if settings.supabase_jwt_secret or settings.supabase_jwks_url:
        sup = decode_supabase_access_token(token)
        if sup is not None:
            email = (sup.get("email") or "").strip()
            sub = str(sup.get("sub") or "")
            username = email or sub
            uname_key = username.lower() if username else ""

            # Enforce allowlist: only pre-registered users in app_users may
            # access the API.  Generic 403 — never reveal the list to callers.
            row = db.execute(
                text(
                    "SELECT id, role FROM app_users"
                    " WHERE LOWER(username) = LOWER(:key)"
                ),
                {"key": uname_key},
            ).mappings().first()
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access not authorized. Contact your administrator.",
                )

            role = str(row["role"])

            logger.debug(
                "Supabase auth: user=%r  email=%r  db_role=%r",
                username,
                email,
                role,
            )

            allowed = modules_for_username(uname_key, role)
            return {
                "id": sub,
                "username": username,
                "role": role,
                "allowed_modules": allowed,
            }

    # ------------------------------------------------------------------
    # Legacy internal-JWT path  (app_users table)
    # ------------------------------------------------------------------
    try:
        payload = decode_token(token)
        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid token. "
                "For Supabase Auth, ensure SUPABASE_URL is set in backend/.env "
                "so the API can verify ES256 access tokens via the JWKS endpoint."
            ),
        )

    row = db.execute(
        text("SELECT id, username, role FROM app_users WHERE username = :u"),
        {"u": username},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    allowed = modules_for_username(row["username"], row["role"])
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "role": row["role"],
        "allowed_modules": allowed,
    }


def require_module(module: str):
    def _inner(user: Annotated[dict, Depends(get_current_user)]) -> dict:
        if module not in user["allowed_modules"]:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _inner
