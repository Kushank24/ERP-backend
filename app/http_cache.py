"""Conditional-request (ETag) support for JSON GET responses.

This is the browser-facing half of the response cache.  `app/cache.py` keeps
the server from re-querying Postgres; this keeps the network from re-sending a
payload the browser already holds.

Deliberately `no-cache` rather than `max-age`: every cached entry is
revalidated against the server, so a `cache_bust()` is reflected immediately.
A positive `max-age` would let a browser keep serving a list the user has
already changed, which no server-side invalidation could reach.  The win here
is bandwidth on the large list and analytics payloads, not fewer requests.
"""

from __future__ import annotations

import hashlib

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Responses are per-user (bearer token) but the browser's HTTP cache keys on
# URL alone, so without this two accounts in one browser could read each
# other's cached payloads.
_VARY = "Authorization"
_CACHE_CONTROL = "private, no-cache"


class ETagMiddleware(BaseHTTPMiddleware):
    """Attach an ETag to JSON GETs and answer matching requests with 304."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if request.method != "GET" or response.status_code != 200:
            return response
        if not request.url.path.startswith("/api/v1"):
            return response

        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
            # PDFs, spreadsheets and images stream from disk or Cloudinary —
            # buffering them here would defeat the streaming response.
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        etag = f'"{hashlib.md5(body).hexdigest()}"'

        if request.headers.get("if-none-match") == etag:
            return Response(
                status_code=304,
                headers={
                    "etag": etag,
                    "cache-control": _CACHE_CONTROL,
                    "vary": _VARY,
                },
                # Rebuilding the response would otherwise drop any task the
                # handler attached — carry it over so it still runs.
                background=response.background,
            )

        headers = MutableHeaders(raw=response.raw_headers)
        headers["etag"] = etag
        headers["cache-control"] = _CACHE_CONTROL
        headers["vary"] = _VARY
        return Response(
            content=body,
            status_code=200,
            headers=dict(headers),
            media_type=content_type,
            background=response.background,
        )
