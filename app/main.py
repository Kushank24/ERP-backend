from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from .config import settings
from .routers import (
    auth,
    dashboard,
    finished_goods,
    materials,
    products,
    purchase_orders,
    sales_orders,
    work_orders,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="E-Safe ERP API", version="1.0.0")

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# ---------------------------------------------------------------------------
# Global database-error handler
# ---------------------------------------------------------------------------
# Without this, any SQLAlchemy connection failure (DNS resolution failure,
# wrong password, network timeout, …) propagates as an unhandled exception
# and FastAPI returns a bare 500 with no useful information.
# This handler catches every SQLAlchemy error and returns a 503 with a
# descriptive message so the frontend (and developer) know exactly what to fix.
# ---------------------------------------------------------------------------

_DB_HELP = (
    "Cannot connect to the database. "
    "Open Supabase Dashboard → Project Settings → Database → "
    "Connection string, choose 'Session mode' (port 5432) or "
    "'Transaction mode' (port 6543) pooler, copy the URI, "
    "replace [YOUR-PASSWORD] with your database password, "
    "change the scheme to postgresql+psycopg://, and set it as "
    "DATABASE_URL in ERP/backend/.env. Then restart uvicorn."
)

_DNS_HELP = (
    "DATABASE_URL points to a host that cannot be resolved. "
    "The direct host db.<ref>.supabase.co is often unreachable due to "
    "IPv6 / DNS issues. Switch to the Supabase connection pooler: "
    "Supabase Dashboard → Project Settings → Database → Connection string → "
    "select 'Session pooler' (port 5432) or 'Transaction pooler' (port 6543). "
    "Use the pooler URI with scheme postgresql+psycopg:// and set it as "
    "DATABASE_URL in ERP/backend/.env, then restart uvicorn."
)

_AUTH_HELP = (
    "Database authentication failed. "
    "Your DATABASE_URL password may have been reset. "
    "Go to Supabase Dashboard → Project Settings → Database, "
    "reset (or copy) the database password, update DATABASE_URL in "
    "ERP/backend/.env with the new credentials, then restart uvicorn."
)

_TENANT_HELP = (
    "The Supabase connection pooler could not find your project (tenant). "
    "Make sure you are using the correct pooler URL from "
    "Supabase Dashboard → Project Settings → Database → Connection string. "
    "The username must be in the format postgres.<project-ref> and the host "
    "must match the region shown in the dashboard "
    "(e.g. aws-0-us-east-1.pooler.supabase.com). "
    "Update DATABASE_URL in ERP/backend/.env and restart uvicorn."
)


def _friendly_db_message(exc: BaseException) -> str:
    """Return a human-readable explanation for common database errors."""
    root = getattr(exc, "orig", exc)
    msg = str(root).lower()

    if "nodename nor servname" in msg or "failed to resolve host" in msg or "name or service not known" in msg:
        return _DNS_HELP
    if "tenant or user not found" in msg:
        return _TENANT_HELP
    if "password authentication failed" in msg or "pg_hba" in msg:
        return _AUTH_HELP
    if "127.0.0.1" in msg and ("54322" in msg or "5432" in msg):
        return (
            "DATABASE_URL still points to the local default (127.0.0.1). "
            "Set DATABASE_URL to your Supabase pooler URI in ERP/backend/.env "
            "and restart uvicorn."
        )
    return _DB_HELP


@app.exception_handler(OperationalError)
async def sqlalchemy_operational_error_handler(
    request: Request, exc: OperationalError
) -> JSONResponse:
    detail = _friendly_db_message(exc)
    logger.error("Database OperationalError on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": detail})


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_generic_error_handler(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    detail = _friendly_db_message(exc)
    logger.error("SQLAlchemyError on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": detail})


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth.router, prefix="/api/v1")
app.include_router(dashboard.router, prefix="/api/v1")
app.include_router(materials.router, prefix="/api/v1")
app.include_router(products.router, prefix="/api/v1")
app.include_router(purchase_orders.router, prefix="/api/v1")
app.include_router(work_orders.router, prefix="/api/v1")
app.include_router(finished_goods.router, prefix="/api/v1")
app.include_router(sales_orders.router, prefix="/api/v1")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
