from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import JWTError, jwk, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError

from .config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JWKS in-memory cache
# ---------------------------------------------------------------------------

_jwks_lock = threading.Lock()
_jwks_cache: dict = {}          # raw JWKS response {"keys": [...]}
_jwks_cache_ts: float = 0.0     # unix timestamp of last successful fetch
_JWKS_TTL: float = 3600.0       # re-fetch after 1 hour


def _fetch_jwks(url: str, timeout: int = 5) -> dict:
    """Fetch a JWKS document from *url* and return the parsed dict."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _get_jwks() -> dict:
    """
    Return the cached JWKS for the configured Supabase project, refreshing
    if the cache is empty or stale.  Thread-safe.
    """
    global _jwks_cache, _jwks_cache_ts

    url = settings.supabase_jwks_url
    if not url:
        return {}

    with _jwks_lock:
        now = time.monotonic()
        if _jwks_cache and (now - _jwks_cache_ts) < _JWKS_TTL:
            return _jwks_cache

        try:
            data = _fetch_jwks(url)
            _jwks_cache = data
            _jwks_cache_ts = now
            logger.debug("JWKS refreshed from %s (%d key(s))", url, len(data.get("keys", [])))
        except Exception as exc:
            if _jwks_cache:
                logger.warning("JWKS refresh failed (%s); using stale cache.", exc)
            else:
                logger.error("JWKS fetch failed and cache is empty: %s", exc)

        return _jwks_cache


def _decode_with_jwks(token: str) -> Optional[dict]:
    """
    Try to verify *token* using the public keys from the project's JWKS
    endpoint.  Returns the decoded payload dict or None if verification fails
    (wrong key, bad signature, expired, …).

    Key selection:
    1. If the token's ``kid`` header matches a key in the JWKS, use that key.
    2. Otherwise try all keys in the set (handles projects with a single key
       that omits ``kid`` in the token header).
    """
    jwks = _get_jwks()
    keys: list[dict] = jwks.get("keys", [])
    if not keys:
        return None

    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        return None

    token_kid: Optional[str] = header.get("kid")
    alg: str = header.get("alg", "ES256")

    # Build the ordered list of candidate keys.
    if token_kid:
        matched = [k for k in keys if k.get("kid") == token_kid]
        candidates = matched if matched else keys
    else:
        candidates = keys

    for key_data in candidates:
        try:
            public_key = jwk.construct(key_data, algorithm=key_data.get("alg", alg))
        except Exception:
            continue

        # First attempt: validate audience claim.
        try:
            return jwt.decode(
                token,
                public_key,
                algorithms=[key_data.get("alg", alg)],
                audience="authenticated",
            )
        except (JWTClaimsError, ExpiredSignatureError):
            # Claims error often means the audience doesn't match (some
            # Supabase projects omit it).  Expired tokens should still fail.
            pass
        except JWTError:
            pass

        # Second attempt: skip audience check.
        try:
            return jwt.decode(
                token,
                public_key,
                algorithms=[key_data.get("alg", alg)],
                options={"verify_aud": False},
            )
        except JWTError:
            pass

    return None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def verify_password(plain: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            plain.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def create_access_token(subject: str, extra: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_exp_hours)
    payload = {"sub": subject, "exp": expire, **extra}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def decode_supabase_access_token(token: str) -> Optional[dict]:
    """
    Validate a Supabase Auth access token.  Returns the decoded payload dict
    on success, or ``None`` if the token cannot be verified.

    Verification order
    ------------------
    1. **ES256 via JWKS** — used by Supabase projects that have migrated to
       the new EC signing keys.  Requires ``SUPABASE_URL`` in ``backend/.env``.
    2. **HS256 via legacy JWT secret** — used by older Supabase projects
       and still required for the *anon* / *service_role* API keys.
       Requires ``SUPABASE_JWT_SECRET`` in ``backend/.env``.

    If neither is configured, ``None`` is returned and the caller falls back
    to the internal API JWT.
    """
    if not token:
        return None

    # --- 1. ES256 / JWKS path -------------------------------------------
    if settings.supabase_jwks_url:
        result = _decode_with_jwks(token)
        if result is not None:
            return result
        # Log at debug level; we may still succeed with the HS256 path.
        logger.debug("JWKS verification did not accept the token; trying HS256 fallback.")

    # --- 2. HS256 / legacy secret path -----------------------------------
    if settings.supabase_jwt_secret:
        for verify_aud in (True, False):
            try:
                opts: dict = {} if verify_aud else {"verify_aud": False}
                return jwt.decode(
                    token,
                    settings.supabase_jwt_secret,
                    algorithms=["HS256"],
                    audience="authenticated",
                    options=opts,
                )
            except JWTError:
                if verify_aud:
                    continue
                # Both attempts failed.
                logger.debug("HS256 verification also failed for this token.")

    return None
