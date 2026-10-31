"""
Grand-total round-off shared by sales orders and purchase orders.

A document stores the user's *intent* (`round_off_mode`) and the signed delta
currently applied (`round_off`). The delta is never taken from the client: it
is always derived here from the mode and the base total the server computed,
so an edit to the lines, the extra costs or the GST rate re-derives a correct
delta instead of carrying a stale one.
"""
from __future__ import annotations

import math
from typing import Literal

RoundOffMode = Literal["none", "down", "up"]

VALID_MODES = ("none", "down", "up")


def compute_round_off(base_total: float, mode: str | None) -> float:
    """
    Signed delta to add to `base_total` so the grand total lands on a whole
    rupee, in the direction `mode` asks for. Always in (-1, 1); 0 when the mode
    is 'none' or the base is already whole.

    Rounded to 2 dp so the stored delta and the stored total stay consistent
    with the 2-dp figures shown everywhere else (a float base like 1180.0000001
    would otherwise produce a delta that does not visibly add up).
    """
    base = round(float(base_total), 2)
    if mode == "down":
        return round(math.floor(base) - base, 2)
    if mode == "up":
        return round(math.ceil(base) - base, 2)
    return 0.0


def normalize_mode(mode: str | None) -> str:
    return mode if mode in VALID_MODES else "none"
