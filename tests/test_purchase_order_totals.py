"""
What `purchase_orders.total_amount` means.

It is the GST-inclusive grand total — lines + extra costs, plus GST on that
sum, plus any round-off — matching sales_orders.total_amount and what the
list's Amount column, the detail screen and the PO PDF all show.

This is pinned because it silently drifted once: create/update/additional-costs
each computed a total of their own and all three had settled on the *pre-GST*
line sum, so editing a purchase order's GST rate changed the rate in the table
and left every stored total exactly as it was. 473 of the 637 purchase orders
carrying a GST rate store a GST-inclusive total; the 119 storing the bare line
sum are the ones those write paths had touched.

The arithmetic lives in _round_off_inputs, which every write path now funnels
through via _recompute_total, so these tests drive that one function with a
stubbed row rather than a database.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.routers.purchase_orders import _round_off_inputs


def _db(lines_total=1000.0, gst_rate=18.0, additional_costs=None, mode="none"):
    """A db whose single SELECT returns one purchase-order row."""
    db = MagicMock()
    db.execute.return_value.mappings.return_value.first.return_value = {
        "gst_rate": gst_rate,
        "round_off_mode": mode,
        "additional_costs": additional_costs if additional_costs is not None else [],
        "lines_total": lines_total,
    }
    return db


def _base(**kw):
    return _round_off_inputs(_db(**kw), po_id=1)[0]


# ---------------------------------------------------------------------------
# GST is part of the total — the regression this file exists for
# ---------------------------------------------------------------------------


def test_total_includes_gst():
    assert _base(lines_total=1000.0, gst_rate=18.0) == pytest.approx(1180.0)


def test_changing_the_gst_rate_changes_the_total():
    """
    The actual bug: the rate moved, the total did not. Three different rates
    over the same lines must give three different totals.
    """
    totals = {gst: _base(lines_total=51810.0, gst_rate=gst) for gst in (0, 5, 12, 18)}
    assert totals[0] == pytest.approx(51810.00)
    assert totals[5] == pytest.approx(54400.50)
    assert totals[12] == pytest.approx(58027.20)
    assert totals[18] == pytest.approx(61135.80)
    assert len(set(totals.values())) == 4


def test_total_is_not_the_bare_line_sum():
    """Pinned directly against the shape the write paths had drifted into."""
    assert _base(lines_total=51810.0, gst_rate=18.0) != pytest.approx(51810.0)


def test_zero_gst_leaves_the_line_sum_alone():
    assert _base(lines_total=1000.0, gst_rate=0.0) == pytest.approx(1000.0)


def test_fractional_gst_rate_is_a_percentage_not_a_fraction():
    """17.5 means 17.5%, never 1750%."""
    assert _base(lines_total=1000.0, gst_rate=17.5) == pytest.approx(1175.0)


# ---------------------------------------------------------------------------
# Extra costs are part of the GST base
# ---------------------------------------------------------------------------


def test_extra_costs_are_taxed_too():
    """
    GST applies to freight and the like, not just the goods — the inline
    computation in the additional-costs endpoint used to omit GST entirely.
    """
    base = _base(
        lines_total=1000.0,
        gst_rate=18.0,
        additional_costs=[{"label": "Freight", "amount": 500.0}],
    )
    assert base == pytest.approx(1770.0)


def test_extra_costs_accept_a_json_string():
    """jsonb can come back already decoded or as text, depending on the driver."""
    base = _round_off_inputs(
        _db(lines_total=1000.0, gst_rate=18.0, additional_costs='[{"label":"Freight","amount":500.0}]'),
        po_id=1,
    )[0]
    assert base == pytest.approx(1770.0)


def test_missing_amount_in_an_extra_cost_does_not_crash():
    base = _base(lines_total=1000.0, gst_rate=18.0, additional_costs=[{"label": "TBC"}])
    assert base == pytest.approx(1180.0)


# ---------------------------------------------------------------------------
# Mode passthrough
# ---------------------------------------------------------------------------


def test_stored_round_off_mode_comes_back():
    assert _round_off_inputs(_db(mode="down"), po_id=1)[1] == "down"


def test_an_unknown_mode_is_normalised_to_none():
    assert _round_off_inputs(_db(mode="sideways"), po_id=1)[1] == "none"


def test_a_missing_purchase_order_is_a_404():
    db = MagicMock()
    db.execute.return_value.mappings.return_value.first.return_value = None
    with pytest.raises(Exception) as exc:
        _round_off_inputs(db, po_id=999)
    assert "404" in str(exc.value) or "not found" in str(exc.value).lower()
