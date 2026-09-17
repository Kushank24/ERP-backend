"""
Unit vocabulary and conversion for BOQ -> inventory stock movements.

Why the alias tables are so wide: units are typed by hand, arrive through CSV
bulk upload, and predate the dropdowns, so production data contains several
spellings of the same unit. A spelling that is *missing* here is worse than an
error — ``convert_qty`` returns None and every caller falls back to the raw
quantity, silently deducting the wrong amount. Live examples found in the data:

  * ``SqMtr`` (28 BOQ lines, 2 materials) alongside a single ``Sq. Meter``
  * ``g`` in 44 BOQ lines against materials held in ``Kg`` — a 1000x
    over-deduction, because no mass table existed at all
  * ``mm`` in 3 lines against materials held in ``m``

Add a spelling here before adding it to a dropdown, never the other way round.

Counts (``Nos``, ``Set``, ``PC``) are deliberately absent: they are
dimensionless and only ever convert to themselves. ``Nos`` and ``Set`` are NOT
treated as interchangeable — a set of four is not four numbers.
"""

from __future__ import annotations

from typing import Optional

# Metres per unit.
_LENGTH_TO_M = {
    "meter": 1.0, "metre": 1.0, "meters": 1.0, "metres": 1.0, "m": 1.0,
    "feet": 0.3048, "ft": 0.3048, "foot": 0.3048,
    "inch": 0.0254, "inches": 0.0254, "in": 0.0254,
    "mm": 0.001, "millimeter": 0.001, "millimetre": 0.001,
    "cm": 0.01, "centimeter": 0.01, "centimetre": 0.01,
}

# Square metres per unit.
_AREA_TO_SQM = {
    "sq. meter": 1.0, "sq.meter": 1.0, "sq meter": 1.0, "square meter": 1.0,
    "sq. metre": 1.0, "sq.metre": 1.0, "sq metre": 1.0, "square metre": 1.0,
    "sqmtr": 1.0, "sq mtr": 1.0, "sq.mtr": 1.0, "sq. mtr": 1.0,
    "sqm": 1.0, "sq m": 1.0, "m2": 1.0,
    "sq. feet": 0.092903, "sq.feet": 0.092903, "sq feet": 0.092903,
    "square feet": 0.092903, "sq. ft": 0.092903, "sq.ft": 0.092903,
    "sqft": 0.092903, "sq ft": 0.092903, "ft2": 0.092903,
}

# Kilograms per unit.
_MASS_TO_KG = {
    "kg": 1.0, "kgs": 1.0, "kilogram": 1.0, "kilograms": 1.0,
    "g": 0.001, "gm": 0.001, "gms": 0.001, "gram": 0.001, "grams": 0.001,
    "mg": 0.000001, "milligram": 0.000001,
    "ton": 1000.0, "tonne": 1000.0, "tonnes": 1000.0, "mt": 1000.0,
    "quintal": 100.0,
}

_TABLES = (_LENGTH_TO_M, _AREA_TO_SQM, _MASS_TO_KG)

#: Units offered in the UI dropdowns. Every entry must be resolvable by
#: ``convert_qty`` against the others in its dimension — ``test_unit_conversion``
#: asserts this, so adding a label here without a table entry fails the suite.
CANONICAL_UNITS = [
    "Nos",
    "Set",
    "Kg",
    "g",
    "Meter",
    "Feet",
    "mm",
    "SqMtr",
    "Sq. Feet",
]


def normalize_unit(unit: str) -> str:
    """Lower-case, whitespace-collapsed key used for every table lookup."""
    return " ".join((unit or "").strip().lower().split())


def dimension_of(unit: str) -> Optional[str]:
    """Return 'length' | 'area' | 'mass' | None (unknown or dimensionless)."""
    key = normalize_unit(unit)
    if key in _LENGTH_TO_M:
        return "length"
    if key in _AREA_TO_SQM:
        return "area"
    if key in _MASS_TO_KG:
        return "mass"
    return None


def convert_qty(qty: float, from_unit: str, to_unit: str) -> Optional[float]:
    """
    Return qty converted from from_unit to to_unit, or None if the two cannot
    be reconciled — unknown spelling, dimensionless, or different dimensions
    (e.g. Meter -> Kg).

    Callers must treat None as "do not guess". Falling back to the raw quantity
    is what produced the 1000x deductions described in the module docstring.
    """
    f = normalize_unit(from_unit)
    t = normalize_unit(to_unit)
    if f == t:
        return qty
    for table in _TABLES:
        if f in table and t in table:
            return qty * table[f] / table[t]
    return None
