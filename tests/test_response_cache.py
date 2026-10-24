"""
Response-cache guarantees — server side (Redis) and browser side (ETag).

These tests exist because the cache shipped half-wired twice over:

- ``upstash_redis`` was never added to requirements.txt, so ``_get_redis()``
  returned None on every call and the whole cache was dead code in production.
- ``analytics.list_companies_summary`` read from the cache but never wrote to
  it, so it paid the lookup on every request and never got a hit.
- ``purchase_orders.delete_po`` committed a delete without a ``cache_bust``,
  so the PO list kept serving the deleted order until its TTL expired.

The first two are read-path bugs that look like working code; the third is a
stale-data bug. All three are invisible at runtime, so they are pinned here by
static analysis over the router modules rather than by exercising endpoints —
a new router or route only fails these when it is genuinely miswired.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi import BackgroundTasks, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.cache import _DERIVED, cache_bust
from app.http_cache import ETagMiddleware

ROUTERS = pathlib.Path(__file__).resolve().parent.parent / "app" / "routers"

MUTATING_DECORATORS = {"post", "put", "patch", "delete"}

#: Mutating handlers in cache-aware routers that legitimately do not bust.
#: Each one either writes nothing a cached read can see, or returns a value
#: derived entirely from its own arguments.
BUST_EXEMPT = {
    # Uploads a file to Cloudinary and returns its identity. The sales-order
    # row is only touched by the PATCH that follows, which busts.
    "upload_document",
    # Sends mail and stamps email_last_sent_at in a background task; the SO
    # list does not surface that column.
    "send_email",
}


def _router_modules():
    for path in sorted(ROUTERS.glob("*.py")):
        if path.name == "__init__.py":
            continue
        yield path.name, ast.parse(path.read_text())


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _calls(node) -> set[str]:
    return {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def _route_methods(node) -> set[str]:
    """HTTP methods this function is decorated with, e.g. {"get"}."""
    methods = set()
    for dec in node.decorator_list:
        func = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in ("router", "public_router"):
                methods.add(func.attr)
    return methods


# ---------------------------------------------------------------------------
# Read path — a lookup without a write is a permanent cache miss
# ---------------------------------------------------------------------------


def test_every_cache_read_has_a_matching_write():
    offenders = []
    for name, tree in _router_modules():
        for fn in _functions(tree):
            calls = _calls(fn)
            if "cache_get" in calls and "cache_set" not in calls:
                offenders.append(f"{name}:{fn.name}")
    assert not offenders, (
        "These handlers check the cache but never populate it, so they can "
        f"never hit: {offenders}"
    )


def test_cached_reads_are_get_only():
    """A cached write endpoint would serve a stale body as its own result."""
    offenders = []
    for name, tree in _router_modules():
        for fn in _functions(tree):
            if "cache_get" not in _calls(fn):
                continue
            methods = _route_methods(fn)
            if methods and not methods <= {"get"}:
                offenders.append(f"{name}:{fn.name} ({sorted(methods)})")
    assert not offenders, f"Non-GET handlers reading from the cache: {offenders}"


# ---------------------------------------------------------------------------
# Write path — a commit without a bust serves stale data until the TTL lapses
# ---------------------------------------------------------------------------


def test_every_mutating_route_in_a_cached_router_busts():
    offenders = []
    for name, tree in _router_modules():
        source_calls = {
            c for fn in _functions(tree) for c in _calls(fn)
        }
        if "cache_bust" not in source_calls:
            continue  # router has no cached reads to invalidate
        for fn in _functions(tree):
            if not _route_methods(fn) & MUTATING_DECORATORS:
                continue
            if fn.name in BUST_EXEMPT:
                continue
            if "cache_bust" not in _calls(fn):
                offenders.append(f"{name}:{fn.name}")
    assert not offenders, (
        "These endpoints mutate state in a router with cached reads but never "
        f"invalidate them: {offenders}"
    )


def test_bust_clears_derived_reporting_namespaces(monkeypatch):
    """Dashboard and analytics aggregate the entity tables, so any entity
    write can move their numbers — busting one prefix must clear them too."""
    seen: list[str] = []

    class _FakePipe:
        def keys(self, pattern):
            seen.append(pattern)

        def exec(self):
            return [[] for _ in seen]

    class _FakeRedis:
        def pipeline(self):
            return _FakePipe()

    monkeypatch.setattr("app.cache._get_redis", lambda: _FakeRedis())
    cache_bust("po", "materials")

    assert "po:*" in seen and "materials:*" in seen
    for derived in _DERIVED:
        assert f"{derived}:*" in seen, f"{derived} not invalidated"


@pytest.mark.parametrize(
    "written,stale,why",
    [
        (
            "materials",
            "products",
            "products:costs sums materials.per_unit_cost over the BOQ, so a "
            "price edit or PO receipt changes a product cost with no write "
            "to products",
        ),
        (
            "catalog",
            "offers",
            "offers:item resolves line names from catalog_products and spec "
            "order from catalog_product_specifications.display_order, so a "
            "catalog edit changes how an existing offer renders",
        ),
    ],
)
def test_bust_cascades_to_dependent_namespaces(monkeypatch, written, stale, why):
    """A namespace that caches a value computed from another namespace's
    tables goes stale without ever being written to."""
    seen: list[str] = []

    class _FakePipe:
        def keys(self, pattern):
            seen.append(pattern)

        def exec(self):
            return [[] for _ in seen]

    class _FakeRedis:
        def pipeline(self):
            return _FakePipe()

    monkeypatch.setattr("app.cache._get_redis", lambda: _FakeRedis())
    cache_bust(written)
    assert f"{stale}:*" in seen, f"busting {written!r} must clear {stale!r}: {why}"


def test_bust_is_a_no_op_without_redis(monkeypatch):
    """The app must stay fully functional with no cache configured."""
    monkeypatch.setattr("app.cache._get_redis", lambda: None)
    cache_bust("po")  # must not raise


# ---------------------------------------------------------------------------
# Browser path — ETag / conditional requests
# ---------------------------------------------------------------------------


#: A stand-in for the real app: same middleware, no database behind it, so
#: these assert middleware behaviour rather than whether Postgres is reachable.
@pytest.fixture
def client():
    probe = FastAPI()
    probe.add_middleware(ETagMiddleware)
    probe.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @probe.get("/api/v1/things")
    def things():
        return [{"id": 1}]

    @probe.get("/api/v1/report.pdf")
    def report():
        return Response(b"%PDF-fake", media_type="application/pdf")

    @probe.post("/api/v1/things")
    def create_thing():
        return {"id": 2}

    @probe.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(probe)


def test_json_get_carries_an_etag(client):
    resp = client.get("/api/v1/things")
    assert resp.status_code == 200
    assert resp.headers["etag"]
    assert "no-cache" in resp.headers["cache-control"]
    # Per-user payloads must not be shared across accounts in one browser.
    assert "Authorization" in resp.headers["vary"]


def test_matching_if_none_match_returns_304(client):
    """The whole point: an unchanged payload is not re-sent."""
    first = client.get("/api/v1/things")
    second = client.get(
        "/api/v1/things", headers={"If-None-Match": first.headers["etag"]}
    )
    assert second.status_code == 304
    assert second.content == b""


def test_changed_payload_gets_a_new_etag(client):
    """A stale validator must not win — the ETag is a hash of the body."""
    stale = client.get("/api/v1/things", headers={"If-None-Match": '"nope"'})
    assert stale.status_code == 200
    assert stale.json() == [{"id": 1}]


def test_304_keeps_its_cors_headers(client):
    """CORS must stay outermost, or the browser rejects the 304 it caused."""
    first = client.get(
        "/api/v1/things", headers={"Origin": "http://localhost:3000"}
    )
    second = client.get(
        "/api/v1/things",
        headers={
            "Origin": "http://localhost:3000",
            "If-None-Match": first.headers["etag"],
        },
    )
    assert second.status_code == 304
    assert second.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_non_json_and_non_api_responses_are_untouched(client):
    """Streamed files must not be buffered to be hashed."""
    assert "etag" not in client.get("/api/v1/report.pdf").headers
    assert "etag" not in client.get("/health").headers


def test_mutations_are_never_given_a_validator(client):
    assert "etag" not in client.post("/api/v1/things").headers


def test_background_tasks_survive_the_rewrite():
    """Rebuilding the response must carry its background task across, both on
    a 200 and on a 304 — dropping it would silently cancel the work."""
    ran: list[str] = []

    probe = FastAPI()
    probe.add_middleware(ETagMiddleware)

    @probe.get("/api/v1/deferred")
    def deferred(tasks: BackgroundTasks):
        tasks.add_task(ran.append, "done")
        return {"queued": True}

    probe_client = TestClient(probe)
    first = probe_client.get("/api/v1/deferred")
    assert first.status_code == 200
    assert ran == ["done"]

    second = probe_client.get(
        "/api/v1/deferred", headers={"If-None-Match": first.headers["etag"]}
    )
    assert second.status_code == 304
    assert ran == ["done", "done"]
