"""
Authorization guarantees for the ERP API.

These tests exist because authorization was previously written but never
wired up: ``require_module`` was defined in ``app/deps.py`` and referenced
nowhere, so every authenticated user could call every endpoint. They lock in
the fix so a future refactor cannot silently reopen the hole.

Each test states the invariant it protects rather than testing an
implementation detail, so adding a router or a route is only a test failure
when the new code is genuinely unauthorized.
"""

from __future__ import annotations

import pytest

from app.main import app
from app.permissions import MODULES, modules_for_role

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

#: The only two endpoints allowed to answer without a bearer token.
#:
#: - ``/auth/login`` must be reachable to obtain a token in the first place.
#: - ``/email-campaigns/images/{filename}`` is embedded as an absolute ``src``
#:   in outbound email HTML; recipients' mail clients send no Authorization
#:   header, so gating it would break images in every campaign already sent.
EXPECTED_PUBLIC = {
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/email-campaigns/images/{filename}"),
}


def _api_routes():
    for r in app.routes:
        path = getattr(r, "path", "")
        if path.startswith("/api/v1"):
            yield r


def _dep_names(route) -> str:
    return " ".join(d.call.__qualname__ for d in route.dependant.dependencies)


def _is_authenticated(route) -> bool:
    names = _dep_names(route)
    return "_inner" in names or "get_current_user" in names


def _strict_gates(route) -> int:
    return sum(
        1
        for d in route.dependant.dependencies
        if d.call.__qualname__.startswith("require_module")
    )


# ---------------------------------------------------------------------------
# Authentication surface
# ---------------------------------------------------------------------------


def test_only_known_endpoints_are_public():
    """No endpoint may be added without a token requirement by accident."""
    public = {
        (m, r.path)
        for r in _api_routes()
        if not _is_authenticated(r)
        for m in (r.methods or set())
        if m != "HEAD"
    }
    assert public == EXPECTED_PUBLIC


def test_debug_endpoint_is_gone():
    """/auth/debug leaked JWT secret length and decoded arbitrary tokens."""
    paths = {r.path for r in _api_routes()}
    assert "/api/v1/auth/debug" not in paths


# ---------------------------------------------------------------------------
# Authorization surface
# ---------------------------------------------------------------------------


def test_every_mutating_route_has_a_strict_module_gate():
    """
    A write must be authorized against the module that owns it — router-level
    breadth (which permits cross-module *reads*) is not sufficient for a write.
    """
    ungated = [
        (sorted(r.methods or []), r.path)
        for r in _api_routes()
        if (r.methods or set()) & MUTATING
        and r.path != "/api/v1/auth/login"
        and _strict_gates(r) == 0
    ]
    assert ungated == []


#: Routes that require a valid token but deliberately carry no module gate,
#: because they are about the caller rather than about business data.
#: ``/auth/me`` echoes the current user — the frontend calls it on load to
#: discover its own ``allowed_modules``, so every authenticated role needs it.
EXPECTED_AUTH_ONLY = {("GET", "/api/v1/auth/me")}


def test_no_route_is_authenticated_but_unauthorized():
    """
    Every route behind a token must also resolve to a module check, except the
    small explicit set above. This is the test that would have failed before
    the fix — it caught all 107 routes.
    """
    auth_only = {
        (m, r.path)
        for r in _api_routes()
        if _is_authenticated(r) and "_inner" not in _dep_names(r)
        for m in (r.methods or set())
        if m != "HEAD"
    }
    assert auth_only == EXPECTED_AUTH_ONLY


# ---------------------------------------------------------------------------
# Role resolution — the substring-escalation fix
# ---------------------------------------------------------------------------


def test_username_based_role_resolution_is_removed():
    """
    Roles used to be inferred from the e-mail local part, so
    ``sales.admin@evil.com`` resolved to admin. Only app_users.role is trusted.
    """
    import app.permissions as perms

    assert not hasattr(perms, "modules_for_username")


@pytest.mark.parametrize("role", ["", "   ", "superuser", "root", "Administrator", "unknown"])
def test_unrecognised_roles_degrade_to_viewer(role):
    """A bad or missing role must never widen access."""
    assert modules_for_role(role) == ["dashboard"]


def test_none_role_degrades_to_viewer():
    assert modules_for_role(None) == ["dashboard"]


def test_role_lookup_is_case_insensitive():
    assert modules_for_role("ADMIN") == modules_for_role("admin")


def test_returned_module_list_is_a_copy():
    """A caller mutating its list must not widen the shared role definition."""
    first = modules_for_role("viewer")
    first.append("offers")
    assert modules_for_role("viewer") == ["dashboard"]


def test_admin_covers_every_module():
    assert set(modules_for_role("admin")) == set(MODULES)


def test_viewer_cannot_reach_any_write_module():
    viewer = set(modules_for_role("viewer"))
    assert viewer == {"dashboard"}


# ---------------------------------------------------------------------------
# Lockout regression — the roles that exist in production today
# ---------------------------------------------------------------------------

#: Routers each screen reads from, derived from the frontend's actual API calls.
#: A screen a role can open must be able to load its own dropdowns and lookups.
PAGE_DEPENDENCIES = {
    "dashboard":            ["dashboard"],
    "companies":            ["companies"],
    "enquiries":            ["enquiries", "companies"],
    "offers":               ["offers", "companies", "enquiries", "catalog-products"],
    "product_catalog":      ["catalog-products"],
    "crm_analytics":        ["analytics", "companies"],
    "purchase_orders":      ["purchase-orders", "materials"],
    "po_analytics":         ["analytics", "purchase-orders"],
    "sales_orders":         ["sales-orders", "finished-goods"],
    "so_analytics":         ["analytics", "sales-orders"],
    "inventory":            ["materials"],
    "products_boq":         ["products", "materials"],
    "pricing":              ["products"],
    "work_orders":          ["work-orders", "products"],
    "finished_goods":       ["finished-goods", "work-orders"],
    "production_analytics": ["analytics", "work-orders"],
    "email_campaigns":      ["email-campaigns"],
    "settings":             [],
}

#: Roles present in app_users at the time of the authorization fix.
PRODUCTION_ROLES = ["admin", "manager", "operator", "offer_maker"]


def _router_gate_modules(prefix: str) -> set[str]:
    """Union of modules accepted by any GET route under /api/v1/<prefix>."""
    accepted: set[str] = set()
    for r in _api_routes():
        if not r.path.startswith(f"/api/v1/{prefix}"):
            continue
        if "GET" not in (r.methods or set()):
            continue
        for d in r.dependant.dependencies:
            closure = getattr(d.call, "__closure__", None) or ()
            for cell in closure:
                val = cell.cell_contents
                if isinstance(val, frozenset):
                    accepted |= set(val)
                elif isinstance(val, str) and val in MODULES:
                    accepted.add(val)
    return accepted


@pytest.mark.parametrize("role", PRODUCTION_ROLES)
def test_role_can_reach_every_router_its_screens_need(role):
    """
    Regression guard against lockout: for each screen this role may open, every
    router that screen reads from must accept at least one of the role's
    modules. This is what makes the router-level gate deliberately broader
    than the screen's own module.
    """
    allowed = set(modules_for_role(role))
    blocked = []
    for page in sorted(allowed):
        for prefix in PAGE_DEPENDENCIES.get(page, []):
            gate = _router_gate_modules(prefix)
            if gate and gate.isdisjoint(allowed):
                blocked.append(f"{page} -> /{prefix}")
    assert blocked == []


def test_every_module_name_used_in_gates_is_a_real_module():
    """A typo in a gate string would silently deny everyone."""
    referenced: set[str] = set()
    for r in _api_routes():
        for d in r.dependant.dependencies:
            closure = getattr(d.call, "__closure__", None) or ()
            for cell in closure:
                val = cell.cell_contents
                if isinstance(val, frozenset):
                    referenced |= {v for v in val if isinstance(v, str)}
                elif isinstance(val, str):
                    referenced.add(val)
    unknown = {m for m in referenced if m not in MODULES}
    assert unknown == set()
