from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)

_redis = None
_redis_attempted = False


def _get_redis():
    global _redis, _redis_attempted
    if _redis_attempted:
        return _redis
    _redis_attempted = True
    try:
        from .config import settings
        url = settings.upstash_redis_rest_url
        token = settings.upstash_redis_rest_token
        if not url or not token:
            return None
        from upstash_redis import Redis
        _redis = Redis(url=url, token=token)
        logger.info("Upstash Redis cache enabled")
    except Exception as exc:
        logger.warning("Redis cache unavailable: %s", exc)
        _redis = None
    return _redis


class _Encoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def cache_get(key: str) -> Optional[Any]:
    r = _get_redis()
    if r is None:
        return None
    try:
        val = r.get(key)
        return json.loads(val) if val is not None else None
    except Exception as exc:
        logger.debug("cache_get(%s) failed: %s", key, exc)
        return None


def cache_set(key: str, value: Any, ttl: int = 300) -> None:
    r = _get_redis()
    if r is None:
        return
    try:
        r.set(key, json.dumps(value, cls=_Encoder), ex=ttl)
    except Exception as exc:
        logger.debug("cache_set(%s) failed: %s", key, exc)


# Namespaces holding aggregates derived from the core entity tables.  Any
# entity mutation can move those numbers, so cache_bust() always clears them
# alongside the prefixes it was given — writes are rare next to reads, and a
# dashboard that disagrees with the list page is worse than one recompute.
_DERIVED = ("analytics", "dashboard")

# Namespaces that cache a value computed from *another* namespace's tables, so
# a write there goes stale without ever touching its own.  Declared once here
# rather than at each call site: the bust that keeps products:costs honest sits
# in the materials and purchase-order routers, where nothing hints that a
# product cache depends on it.
#
#   products:costs:*  joins materials on name and sums m.per_unit_cost, so any
#   price edit, PO receipt or newly-created material changes a BOQ cost — and
#   whether the row is flagged as missing a price — with no write to products.
#
#   offers:item:*     resolves each line's name and definition from
#   catalog_products, and orders its specs by catalog_product_specifications.
#   display_order, so renaming a catalog product or reordering its specs
#   changes how an existing offer renders without writing to offers.
_CASCADE = {
    "materials": ("products",),
    "catalog": ("offers",),
}


def cache_bust(*prefixes: str) -> None:
    """Delete every key under each prefix — call after any mutation.

    Accepts several prefixes so a write that crosses modules (a work order
    that consumes stock, say) clears them in one pair of round-trips rather
    than one pair each.  The derived reporting namespaces are always included,
    as is anything _CASCADE declares downstream of a prefix given.
    """
    r = _get_redis()
    if r is None:
        return
    cascaded = [d for p in prefixes for d in _CASCADE.get(p, ())]
    targets = list(dict.fromkeys([*prefixes, *cascaded, *_DERIVED]))
    try:
        pipe = r.pipeline()
        for prefix in targets:
            pipe.keys(f"{prefix}:*")
        keys = [k for group in pipe.exec() for k in (group or [])]
        if keys:
            r.delete(*keys)
    except Exception as exc:
        logger.debug("cache_bust(%s) failed: %s", ",".join(targets), exc)
