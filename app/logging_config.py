"""
Application logging.

Until this module existed the app configured no logging at all: every module
did ``logger = logging.getLogger(__name__)`` and nothing ever called
``basicConfig`` or attached a handler. With no root handler Python falls back
to ``logging.lastResort``, which emits at WARNING and above and drops the
message format. The practical effect in production was that every
``logger.info`` call vanished — including the one line that says whether a
campaign chose Resend or SMTP, which is exactly what you need to diagnose a
delivery failure.

Call ``configure_logging()`` once at import of ``app.main``, before any
request is served.

LOG_LEVEL controls verbosity (default INFO). Set LOG_LEVEL=DEBUG to include
full request/response detail from the email sender.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    """Attach a stdout handler to the root logger. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    # Replace any handler a host platform installed, so the format is
    # predictable and messages are not emitted twice.
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Our own package should always honour LOG_LEVEL even if a host platform
    # reconfigures the root logger afterwards.
    logging.getLogger("app").setLevel(level)

    # uvicorn installs its own handlers; let them propagate to ours instead so
    # application and server logs interleave in one stream with one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    # SQLAlchemy is very chatty at INFO and would bury everything else.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "Logging configured at %s (set LOG_LEVEL to change)", level_name
    )
