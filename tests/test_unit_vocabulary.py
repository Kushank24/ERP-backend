"""
The stock unit vocabulary, and the spellings that actually exist in the data.

A unit label offered in a dropdown but absent from the conversion tables is a
silent data-corruption bug, not a cosmetic gap: ``convert_qty`` returns None
and callers fall back to the raw quantity, deducting the wrong amount with no
error. These tests pin the vocabulary to the real spellings found in
production, and assert the dropdown list and the conversion tables agree.

Real spellings counted in the live database when this was written:

  materials.unit            Nos 786, m 175, Kg 60, Set 8, ft 6, Meter 4,
                            mm 3, SqMtr 2, "Sq. Meter" 1
  bill_of_quantities.units   Nos 10600, m 5206, Kg 716, Meter 385, g 44,
                            SqMtr 28, Set 10, ft 9, mm 6
  purchase_order_lines.unit  Nos 672, m 423, Kg 106, ft 9, Meter 5, SqMtr 5,
                            Feet 3, mm 2, "Sq. Meter" 2, Set 1
"""

from __future__ import annotations

import pytest

from app.unit_conversion import (
    CANONICAL_UNITS,
    convert_qty,
    dimension_of,
    normalize_unit,
)


# ---------------------------------------------------------------------------
# The dropdown list and the conversion tables must not drift apart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", CANONICAL_UNITS)
def test_every_offered_unit_converts_to_itself(unit):
    assert convert_qty(5.0, unit, unit) == 5.0


@pytest.mark.parametrize("unit", CANONICAL_UNITS)
def test_every_offered_unit_is_either_dimensioned_or_a_known_count(unit):
    """
    A unit must be in a conversion table, or be one of the dimensionless counts
    we deliberately excluded. Anything else is a label with no conversion
    behind it — the exact bug this module guards.
    """
    assert dimension_of(unit) is not None or unit in {"Nos", "Set"}


def test_offered_units_within_a_dimension_all_interconvert():
    by_dim: dict[str, list[str]] = {}
    for u in CANONICAL_UNITS:
        d = dimension_of(u)
        if d:
            by_dim.setdefault(d, []).append(u)
    for dim, units in by_dim.items():
        for a in units:
            for b in units:
                assert convert_qty(1.0, a, b) is not None, f"{dim}: {a} -> {b}"


# ---------------------------------------------------------------------------
# SqMtr — the unit that prompted this change
# ---------------------------------------------------------------------------


def test_sqmtr_is_offered():
    assert "SqMtr" in CANONICAL_UNITS


@pytest.mark.parametrize(
    "spelling",
    ["SqMtr", "sqmtr", "SQMTR", "Sq Mtr", "sq.mtr", "sq. mtr",
     "Sq. Meter", "sq.meter", "square meter", "sqm", "m2", "Sq Metre"],
)
def test_every_sqmtr_spelling_in_the_data_resolves_to_area(spelling):
    """
    The data contains both "SqMtr" (28 BOQ lines, 2 materials) and
    "Sq. Meter" (1 material, 2 PO lines). Both must convert, or one of them
    silently falls back to raw quantities.
    """
    assert dimension_of(spelling) == "area"


def test_sqmtr_and_sq_meter_are_the_same_unit():
    """Different spellings of the same unit must be a 1:1 conversion."""
    assert convert_qty(7.0, "SqMtr", "Sq. Meter") == pytest.approx(7.0)
    assert convert_qty(7.0, "Sq. Meter", "SqMtr") == pytest.approx(7.0)


def test_sqmtr_to_sq_feet():
    assert convert_qty(1.0, "SqMtr", "Sq. Feet") == pytest.approx(10.7639, rel=1e-4)


def test_sqmtr_does_not_convert_to_a_length():
    """Area against length is a data error, not something to guess at."""
    assert convert_qty(1.0, "SqMtr", "Meter") is None


# ---------------------------------------------------------------------------
# Mass — there was no mass table at all
# ---------------------------------------------------------------------------


def test_grams_to_kilograms():
    """
    44 BOQ lines are in `g` against materials held in `Kg`. With no mass table
    convert_qty returned None, the caller fell back to the raw quantity, and
    500 g was deducted as 500 Kg — a 1000x over-deduction.
    """
    assert convert_qty(500.0, "g", "Kg") == pytest.approx(0.5)


def test_kilograms_to_grams():
    assert convert_qty(0.5, "Kg", "g") == pytest.approx(500.0)


@pytest.mark.parametrize("spelling", ["Kg", "kg", "KG", "kgs", "kilogram", "kilograms"])
def test_kilogram_spellings(spelling):
    assert dimension_of(spelling) == "mass"


@pytest.mark.parametrize("spelling", ["g", "G", "gm", "gms", "gram", "grams"])
def test_gram_spellings(spelling):
    assert dimension_of(spelling) == "mass"


def test_tonne_to_kilograms():
    assert convert_qty(2.0, "tonne", "Kg") == pytest.approx(2000.0)


def test_mass_does_not_convert_to_length():
    """`m -> Kg` appears on 14 BOQ lines and must stay unresolvable."""
    assert convert_qty(1.0, "Meter", "Kg") is None
    assert convert_qty(1.0, "m", "Kg") is None


# ---------------------------------------------------------------------------
# Millimetres — 1000x class, same as grams
# ---------------------------------------------------------------------------


def test_millimetres_to_metres():
    """3 BOQ lines pair mm with m and previously fell back to raw."""
    assert convert_qty(1500.0, "mm", "m") == pytest.approx(1.5)


def test_metres_to_millimetres():
    assert convert_qty(1.5, "m", "mm") == pytest.approx(1500.0)


def test_centimetres_to_metres():
    assert convert_qty(250.0, "cm", "Meter") == pytest.approx(2.5)


def test_mm_and_meter_are_the_same_dimension():
    assert dimension_of("mm") == dimension_of("Meter") == "length"


# ---------------------------------------------------------------------------
# Counts stay dimensionless and non-interchangeable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["Nos", "Set"])
def test_counts_have_no_dimension(unit):
    assert dimension_of(unit) is None


def test_nos_and_set_are_not_interchangeable():
    """
    4 BOQ lines pair Nos with Set. A set of four is not four numbers, so there
    is no honest ratio — this must stay unresolvable rather than assume 1:1.
    """
    assert convert_qty(1.0, "Nos", "Set") is None
    assert convert_qty(1.0, "Set", "Nos") is None


def test_a_count_still_converts_to_itself():
    assert convert_qty(12.0, "Nos", "Nos") == 12.0


def test_counts_do_not_convert_to_dimensioned_units():
    for other in ["Kg", "Meter", "SqMtr", "g", "mm"]:
        assert convert_qty(1.0, "Nos", other) is None
        assert convert_qty(1.0, other, "Nos") is None


# ---------------------------------------------------------------------------
# Normalisation — the data has stray whitespace and mixed case
# ---------------------------------------------------------------------------


def test_normalize_trims_and_lowercases():
    assert normalize_unit("  Sq. Meter  ") == "sq. meter"


def test_normalize_collapses_internal_whitespace():
    assert normalize_unit("Sq   Mtr") == "sq mtr"


def test_trailing_whitespace_in_the_data_still_converts():
    """bill_of_quantities contains one row with `Nos ` (trailing space)."""
    assert convert_qty(3.0, "Nos ", "Nos") == 3.0


def test_case_insensitive_across_the_whole_vocabulary():
    assert convert_qty(1.0, "METER", "m") == pytest.approx(1.0)
    assert convert_qty(1.0, "FEET", "ft") == pytest.approx(1.0)


@pytest.mark.parametrize("bad", ["", "   ", None, "widget", "dozen", "litre"])
def test_unknown_units_are_unresolvable_not_guessed(bad):
    assert dimension_of(bad) is None
    assert convert_qty(1.0, bad, "Kg") is None


# ---------------------------------------------------------------------------
# Every spelling observed in production must be resolvable
# ---------------------------------------------------------------------------

#: (spelling, expected dimension) for every distinct value found in
#: materials.unit, bill_of_quantities.units and purchase_order_lines.unit.
OBSERVED_IN_DATA = [
    ("Nos", None), ("Set", None),
    ("m", "length"), ("Meter", "length"), ("ft", "length"),
    ("Feet", "length"), ("mm", "length"),
    ("Kg", "mass"), ("g", "mass"),
    ("SqMtr", "area"), ("Sq. Meter", "area"),
]


@pytest.mark.parametrize("spelling,expected", OBSERVED_IN_DATA)
def test_observed_spelling_resolves_as_expected(spelling, expected):
    assert dimension_of(spelling) == expected


def test_the_373_line_meter_to_m_pair_is_a_noop():
    """The most common real conversion in the data, by a wide margin."""
    assert convert_qty(4.25, "Meter", "m") == pytest.approx(4.25)


# ---------------------------------------------------------------------------
# The frontend dropdown and the backend table are the same list
# ---------------------------------------------------------------------------


def _frontend_stock_units() -> list[str] | None:
    """Parse STOCK_UNITS out of web/lib/units.ts, or None if unavailable."""
    import pathlib
    import re

    ts = (
        pathlib.Path(__file__).resolve().parents[2]
        / "web" / "lib" / "units.ts"
    )
    if not ts.exists():
        return None
    m = re.search(r"export const STOCK_UNITS = \[(.*?)\] as const;", ts.read_text(), re.S)
    if not m:
        return None
    return re.findall(r'"([^"]+)"', m.group(1))


def test_frontend_dropdown_matches_the_backend_vocabulary():
    """
    The four unit dropdowns (inventory, products/BOQ, purchase orders) all read
    web/lib/units.ts. If that list and CANONICAL_UNITS diverge, a user can pick
    a unit the backend cannot convert — which is how this whole class of bug
    started, with four separately-declared lists that had drifted apart.
    """
    frontend = _frontend_stock_units()
    if frontend is None:
        pytest.skip("web/lib/units.ts not present in this checkout")
    assert frontend == CANONICAL_UNITS
