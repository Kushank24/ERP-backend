"""Role → module access (parity with Streamlit get_user_navigation_options)."""

MODULES = [
    "dashboard",
    "companies",
    "enquiries",
    "offers",
    "product_catalog",
    "purchase_orders",
    "sales_orders",
    "inventory",
    "products_boq",
    "pricing",
    "work_orders",
    "finished_goods",
    "settings",
]

_ROLE_MODULES: dict[str, list[str]] = {
    "admin": MODULES.copy(),
    "manager": [
        "dashboard",
        "companies",
        "enquiries",
        "offers",
        "product_catalog",
        "purchase_orders",
        "sales_orders",
        "inventory",
        "products_boq",
        "work_orders",
        "finished_goods",
    ],
    "operator": [
        "dashboard",
        "companies",
        "enquiries",
        "offers",
        "product_catalog",
        "inventory",
        "work_orders",
        "finished_goods",
    ],
    "offer_maker": [
        "dashboard",
        "companies",
        "enquiries",
        "offers",
        "product_catalog",
        "products_boq",
    ],
    "purchase_manager": ["dashboard", "purchase_orders", "inventory", "products_boq"],
    "sales_manager": ["dashboard", "companies", "enquiries", "offers", "sales_orders", "finished_goods", "products_boq"],
    "production_manager": [
        "dashboard",
        "inventory",
        "products_boq",
        "work_orders",
        "finished_goods",
    ],
    "inventory_clerk": ["dashboard", "inventory", "finished_goods"],
    "viewer": ["dashboard"],
}


def modules_for_role(role: str) -> list[str]:
    return list(_ROLE_MODULES.get(role, _ROLE_MODULES["viewer"]))


def modules_for_username(username: str, role: str) -> list[str]:
    u = (username or "").strip().lower()

    # Support both plain usernames ("kushank") and email addresses
    # ("kushank@esafe.com").  When an e-mail is supplied we also try the
    # local part (everything before the first "@") so that Supabase Auth
    # users whose email begins with a known username get the right modules.
    u_local = u.split("@")[0] if "@" in u else u

    overrides: dict[str, list[str]] = {
        "kushank": _ROLE_MODULES["admin"],
        "user1": _ROLE_MODULES["manager"],
        "sarah_manager": _ROLE_MODULES["manager"],
        "mike_purchase": _ROLE_MODULES["purchase_manager"],
        "anna_sales": _ROLE_MODULES["sales_manager"],
        "user2": _ROLE_MODULES["production_manager"],
        "lisa_inventory": _ROLE_MODULES["inventory_clerk"],
        "guest": _ROLE_MODULES["viewer"],
    }

    # Exact match on the full value first, then on the local part.
    if u in overrides:
        return list(overrides[u])
    if u_local in overrides:
        return list(overrides[u_local])

    # Pattern-based fallback (works on both full address and local part).
    check = u_local  # patterns are more meaningful on the local part
    if "admin" in check:
        return _ROLE_MODULES["admin"].copy()
    if "manager" in check and "purchase" not in check and "sales" not in check:
        return _ROLE_MODULES["manager"].copy()
    if "purchase" in check:
        return _ROLE_MODULES["purchase_manager"].copy()
    if "sales" in check:
        return _ROLE_MODULES["sales_manager"].copy()
    if "production" in check:
        return _ROLE_MODULES["production_manager"].copy()
    if "inventory" in check:
        return _ROLE_MODULES["inventory_clerk"].copy()
    if "guest" in check or "view" in check:
        return _ROLE_MODULES["viewer"].copy()

    # Last resort: use the role claim from the JWT / user record.
    return modules_for_role(role)
