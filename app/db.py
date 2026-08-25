from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings


def _create_engine():
    """Supabase Postgres expects TLS; local Postgres usually does not."""
    url = settings.database_url
    connect_args: dict = {}
    if (
        ("supabase.co" in url or "supabase.com" in url)
        and "sslmode" not in url.lower()
    ):
        connect_args["sslmode"] = "require"
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
