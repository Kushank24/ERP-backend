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
from .permissions import modules_for_role
from .security import decode_supabase_access_token, decode_token

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


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

            allowed = modules_for_role(role)
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

    allowed = modules_for_role(row["role"])
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "role": row["role"],
        "allowed_modules": allowed,
    }


def require_module(module: str):
    """
    Authorize a single module. Use on every mutating route so a write is only
    accepted from a role that owns that module.
    """

    def _inner(user: Annotated[dict, Depends(get_current_user)]) -> dict:
        if module not in user["allowed_modules"]:
            logger.warning(
                "Authorization denied: user=%r role=%r module=%r",
                user.get("username"),
                user.get("role"),
                module,
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _inner


def require_any_module(modules: list[str]):
    """
    Authorize if the user holds *any* of ``modules``.

    Applied at router level so legitimate cross-module reads keep working — the
    Offers screen needs to read companies and enquiries, the Purchase Orders
    screen needs to read materials, and so on. Mutating routes carry an
    additional ``require_module`` for the owning module on top of this.
    """
    allowed_set = frozenset(modules)

    def _inner(user: Annotated[dict, Depends(get_current_user)]) -> dict:
        if allowed_set.isdisjoint(user["allowed_modules"]):
            logger.warning(
                "Authorization denied: user=%r role=%r needed any of %r",
                user.get("username"),
                user.get("role"),
                modules,
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _inner
