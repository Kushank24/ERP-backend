from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# Always load backend/.env even if uvicorn is started from another cwd.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:54322/postgres"
    jwt_secret: str = "change-me-in-production-use-openssl-rand"
    jwt_algorithm: str = "HS256"
    jwt_exp_hours: int = 24
    cors_origins: str = "http://localhost:3000"

    # ------------------------------------------------------------------ #
    # Supabase                                                             #
    # ------------------------------------------------------------------ #

    # Project URL — e.g. https://xxxxxxxxxxxxxxxxxxxx.supabase.co
    # Used to derive the JWKS endpoint for ES256 token verification.
    supabase_url: str = ""

    # Legacy HS256 JWT secret (Project Settings → API → JWT Settings →
    # "Legacy JWT Secret").  Still needed for the anon / service-role keys
    # and as a fallback verifier.
    supabase_jwt_secret: str = ""

    # Default role assigned to Supabase Auth users who do not have
    # ``user_metadata.role`` or ``app_metadata.role`` set.
    # For a private/single-tenant ERP set this to "admin" so every
    # authenticated user gets full access unless explicitly restricted.
    # Options: admin | manager | purchase_manager | sales_manager |
    #          production_manager | inventory_clerk | viewer
    supabase_default_role: str = "admin"

    # ------------------------------------------------------------------ #
    # SMTP (bulk email campaigns)                                          #
    # ------------------------------------------------------------------ #
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_delay_seconds: float = 2.0  # pause between emails; 2s ≈ 1800/hr

    # Public base URL used to build absolute image URLs embedded in emails.
    # Set this to the server's public address, e.g. https://erp.esafe.co.in
    base_url: str = "http://localhost:8000"

    # Resend API key (https://resend.com). When set, emails are sent via the
    # Resend HTTP API instead of raw SMTP — required on hosts that block
    # outbound SMTP ports (e.g. Railway).
    resend_api_key: str = ""

    # Comma-separated list of Supabase user e-mail addresses that always
    # receive admin access, regardless of their metadata role.
    # Example:  SUPABASE_ADMIN_EMAILS=alice@example.com,bob@example.com
    supabase_admin_emails: str = ""

    @property
    def supabase_admin_email_set(self) -> set[str]:
        """Lower-cased set of admin e-mails parsed from the env var."""
        return {
            e.strip().lower()
            for e in self.supabase_admin_emails.split(",")
            if e.strip()
        }

    @property
    def supabase_jwks_url(self) -> Optional[str]:
        """
        Returns the JWKS discovery URL for this Supabase project, or None if
        ``SUPABASE_URL`` is not configured.

        Supabase projects that have migrated to the new EC signing keys
        (ES256) publish their public key set at:
            {SUPABASE_URL}/auth/v1/.well-known/jwks.json
        """
        url = self.supabase_url.rstrip("/")
        if not url:
            return None
        return f"{url}/auth/v1/.well-known/jwks.json"


settings = Settings()
