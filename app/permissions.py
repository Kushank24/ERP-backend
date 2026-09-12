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
    "email_campaigns",
    "crm_analytics",
    "production_analytics",
    "po_analytics",
    "so_analytics",
    "settings",
]

_ROLE_MODULES: dict[str, list[str]] = {
    "admin": MODULES.copy(),  # admin gets email_campaigns via MODULES.copy()
    "manager": [
        "dashboard",
        "purchase_orders",
        "sales_orders",
        "inventory",
        "products_boq",
        "work_orders",
        "finished_goods",
        "production_analytics",
        "po_analytics",
        "so_analytics",
    ],
    "operator": [
        "dashboard",
        "companies",
        "enquiries",
        "offers",
        "product_catalog",
        "inventory",
        "products_boq",
        "work_orders",
        "finished_goods",
        "crm_analytics",
        "production_analytics",
    ],
    "offer_maker": [
        "dashboard",
        "companies",
        "enquiries",
        "offers",
        "product_catalog",
        "crm_analytics",
    ],
    "purchase_manager": ["dashboard", "purchase_orders", "inventory", "products_boq", "production_analytics", "po_analytics"],
    "sales_manager": ["dashboard", "companies", "enquiries", "offers", "sales_orders", "finished_goods", "products_boq", "pricing", "crm_analytics", "so_analytics"],
    "production_manager": [
        "dashboard",
        "inventory",
        "products_boq",
        "work_orders",
        "finished_goods",
        "production_analytics",
    ],
    "inventory_clerk": ["dashboard", "inventory", "finished_goods"],
    "viewer": ["dashboard"],
}


#: Roles that exist. Anything not in this set is treated as "viewer".
ROLES = frozenset(_ROLE_MODULES)


def modules_for_role(role: str) -> list[str]:
    """
    Resolve the module allowlist for a role.

    ``role`` comes from ``app_users.role`` — the only trusted source. An
    unknown, empty or NULL role degrades to ``viewer`` (dashboard only) rather
    than to a permissive default, so a bad data row cannot grant access.
    """
    key = (role or "").strip().lower()
    return list(_ROLE_MODULES.get(key, _ROLE_MODULES["viewer"]))
