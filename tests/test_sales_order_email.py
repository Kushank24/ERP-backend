"""
The cost breakdown in the customer-facing sales-order email.

The email used to print the product lines and then a single "Order Total"
that silently folded in the extra costs and the GST, so a customer adding up
the rows in front of them could not reach the figure being asked for — a
packing charge of ₹2,682 plus ₹16,574.76 of GST simply appeared out of
nowhere. Every component is now a named row.

Two judgement calls pinned here:
  - the grand total is derived from the rows printed, not read from
    total_amount, so the visible arithmetic always adds up (and matches the
    order's detail screen, which composes it the same way);
  - GST applies to the extra costs as well as the goods.
"""

from __future__ import annotations

import re

import pytest

from app.routers.sales_orders import _build_so_email_html, _summary_rows


def _so(lines=None, extras=None, gst_rate=18.0, total_amount=1180.0, round_off=0.0):
    return {
        "invoice_number": "GSTNB/26-27/001",
        "company_name": "Acme Industries",
        "gst_rate": gst_rate,
        "total_amount": total_amount,
        "round_off": round_off,
        "additional_costs": extras if extras is not None else [],
        "lines": lines if lines is not None else [
            {"product_name": "Widget", "product_code": "W-1", "quantity_sold": 2,
             "unit_price": 500.0, "total_price": 1000.0},
        ],
    }


def _text(html: str) -> str:
    """Tags stripped, so assertions read like what a recipient sees."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _rows(so, goods_subtotal):
    return _text("".join(_summary_rows(so, goods_subtotal)))


# ---------------------------------------------------------------------------
# Extra costs appear, by their own label
# ---------------------------------------------------------------------------


def test_packing_charges_appear_as_their_own_row():
    out = _rows(_so(extras=[{"label": "PACKING CHARGES 3%", "amount": 2682.0}]), 89400.0)
    assert "PACKING CHARGES 3%" in out
    assert "₹2,682.00" in out


def test_every_extra_cost_gets_its_own_row():
    out = _rows(
        _so(extras=[
            {"label": "Packing Charges", "amount": 500.0},
            {"label": "Freight", "amount": 250.0},
            {"label": "Insurance", "amount": 125.0},
        ]),
        1000.0,
    )
    for label, amount in (("Packing Charges", "₹500.00"), ("Freight", "₹250.00"), ("Insurance", "₹125.00")):
        assert label in out and amount in out


def test_an_extra_cost_with_no_label_still_shows_as_a_charge():
    out = _rows(_so(extras=[{"amount": 300.0}]), 1000.0)
    assert "Other Charges" in out
    assert "₹300.00" in out


def test_zero_value_extras_are_dropped_rather_than_printed_as_blank_rows():
    out = _rows(_so(extras=[{"label": "Freight", "amount": 0.0}]), 1000.0)
    assert "Freight" not in out


def test_an_extra_cost_label_is_html_escaped():
    html = "".join(_summary_rows(_so(extras=[{"label": "Packing & Crating", "amount": 10.0}]), 100.0))
    assert "Packing &amp; Crating" in html


# ---------------------------------------------------------------------------
# The arithmetic a customer can follow
# ---------------------------------------------------------------------------


def test_gst_is_charged_on_the_extras_as_well_as_the_goods():
    """₹89,400 goods + ₹2,682 packing at 18% = ₹16,574.76, not ₹16,092."""
    out = _rows(_so(extras=[{"label": "PACKING CHARGES 3%", "amount": 2682.0}]), 89400.0)
    assert "₹16,574.76" in out


def test_the_rows_add_up_to_the_grand_total():
    out = _rows(_so(extras=[{"label": "PACKING CHARGES 3%", "amount": 2682.0}]), 89400.0)
    # 89,400 + 2,682 + 16,574.76 = 108,656.76
    assert "₹89,400.00" in out
    assert "₹2,682.00" in out
    assert "₹16,574.76" in out
    assert "₹108,656.76" in out


def test_grand_total_is_derived_not_taken_from_a_stale_stored_total():
    """
    16 of the 51 orders with extra costs store a total that omits them, from an
    earlier update_so that recomputed total_amount without additional_costs.
    The email must show the figure its own rows sum to.
    """
    so = _so(extras=[{"label": "PACKING CHARGES 3%", "amount": 2682.0}], total_amount=105492.0)
    out = _rows(so, 89400.0)
    assert "₹108,656.76" in out
    assert "₹105,492.00" not in out


def test_a_stale_stored_total_is_logged(caplog):
    so = _so(extras=[{"label": "Packing", "amount": 2682.0}], total_amount=105492.0)
    with caplog.at_level("WARNING"):
        _summary_rows(so, 89400.0)
    assert "stale stored total" in caplog.text
    assert "GSTNB/26-27/001" in caplog.text


def test_no_warning_when_the_stored_total_agrees(caplog):
    with caplog.at_level("WARNING"):
        _summary_rows(_so(total_amount=1180.0), 1000.0)
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# GST and round-off rows
# ---------------------------------------------------------------------------


def test_gst_row_names_the_rate():
    assert "GST (18%)" in _rows(_so(), 1000.0)


def test_a_fractional_gst_rate_is_not_truncated():
    assert "GST (17.5%)" in _rows(_so(gst_rate=17.5), 1000.0)


def test_a_zero_gst_order_shows_no_gst_row():
    assert "GST" not in _rows(_so(gst_rate=0), 1000.0)


def test_round_off_appears_with_its_sign_when_applied():
    out = _rows(_so(round_off=-0.40), 1000.0)
    assert "Round Off" in out
    assert "−₹0.40" in out


def test_no_round_off_row_when_none_is_applied():
    assert "Round Off" not in _rows(_so(round_off=0.0), 1000.0)


# ---------------------------------------------------------------------------
# The whole email
# ---------------------------------------------------------------------------


def test_the_email_body_carries_the_breakdown_not_a_bare_order_total():
    _, html = _build_so_email_html(
        _so(extras=[{"label": "PACKING CHARGES 3%", "amount": 2682.0}], total_amount=108656.76),
        ["Sales Invoice"],
    )
    body = _text(html)
    assert "PACKING CHARGES 3%" in body
    assert "Grand Total" in body
    # The old single-line summary must not come back alongside the table.
    assert "Order Total:" not in body


def test_subtotal_is_summed_from_the_printed_rows():
    """
    Guards the visible arithmetic against a legacy line whose stored
    total_price disagrees with quantity x unit price.
    """
    so = _so(lines=[
        {"product_name": "Widget", "product_code": None, "quantity_sold": 2,
         "unit_price": 500.0, "total_price": 999_999.0},
    ], total_amount=1180.0)
    _, html = _build_so_email_html(so, [])
    body = _text(html)
    assert "₹1,000.00" in body
    assert "999,999" not in body
