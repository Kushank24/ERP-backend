from __future__ import annotations

from typing import Optional

_LENGTH_TO_M = {
    "meter": 1.0, "m": 1.0,
    "feet": 0.3048, "ft": 0.3048, "foot": 0.3048,
}
_AREA_TO_SQM = {
    "sq. meter": 1.0, "sq.meter": 1.0, "sqm": 1.0, "m2": 1.0, "square meter": 1.0,
    "sq. feet": 0.092903, "sq.feet": 0.092903, "sqft": 0.092903, "square feet": 0.092903,
}


def convert_qty(qty: float, from_unit: str, to_unit: str) -> Optional[float]:
    """Return qty converted from from_unit to to_unit, or None if incompatible."""
    f = (from_unit or "").strip().lower()
    t = (to_unit or "").strip().lower()
    if f == t:
        return qty
    if f in _LENGTH_TO_M and t in _LENGTH_TO_M:
        return qty * _LENGTH_TO_M[f] / _LENGTH_TO_M[t]
    if f in _AREA_TO_SQM and t in _AREA_TO_SQM:
        return qty * _AREA_TO_SQM[f] / _AREA_TO_SQM[t]
    return None
