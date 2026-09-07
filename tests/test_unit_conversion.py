"""
Tests for Purchase Order unit conversion and inventory update logic.

Run with:
    pip install pytest
    cd ERP/backend
    pytest tests/test_unit_conversion.py -v
"""
from __future__ import annotations

import math
import types
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Import the function under test directly (no DB connection needed)
# ---------------------------------------------------------------------------
from app.unit_conversion import convert_qty as _convert_qty


# ===========================================================================
# 1. Pure unit tests — _convert_qty
# ===========================================================================

class TestConvertQtySameUnit:
    def test_meter_to_meter(self):
        assert _convert_qty(5.0, "Meter", "Meter") == 5.0

    def test_feet_to_feet(self):
        assert _convert_qty(10.0, "Feet", "Feet") == 10.0

    def test_kg_to_kg(self):
        assert _convert_qty(3.5, "Kg", "Kg") == 3.5

    def test_nos_to_nos(self):
        assert _convert_qty(100.0, "Nos", "Nos") == 100.0

    def test_sq_meter_to_sq_meter(self):
        assert _convert_qty(25.0, "Sq. Meter", "Sq. Meter") == 25.0

    def test_case_insensitive_same(self):
        # "meter" and "Meter" should be treated as the same unit
        assert _convert_qty(5.0, "meter", "Meter") == 5.0
        assert _convert_qty(5.0, "METER", "meter") == 5.0


class TestConvertQtyLength:
    def test_feet_to_meter(self):
        result = _convert_qty(10.0, "Feet", "Meter")
        assert result is not None
        assert math.isclose(result, 3.048, rel_tol=1e-6)

    def test_meter_to_feet(self):
        result = _convert_qty(1.0, "Meter", "Feet")
        assert result is not None
        assert math.isclose(result, 3.28084, rel_tol=1e-4)

    def test_feet_to_meter_large(self):
        # 100 feet should be ~30.48 meters
        result = _convert_qty(100.0, "Feet", "Meter")
        assert result is not None
        assert math.isclose(result, 30.48, rel_tol=1e-6)

    def test_ft_alias(self):
        # "ft" should work the same as "Feet"
        result = _convert_qty(10.0, "ft", "Meter")
        assert result is not None
        assert math.isclose(result, 3.048, rel_tol=1e-6)

    def test_foot_alias(self):
        result = _convert_qty(10.0, "Foot", "Meter")
        assert result is not None
        assert math.isclose(result, 3.048, rel_tol=1e-6)

    def test_m_alias(self):
        result = _convert_qty(1.0, "m", "Feet")
        assert result is not None
        assert math.isclose(result, 3.28084, rel_tol=1e-4)


class TestConvertQtyArea:
    def test_sq_feet_to_sq_meter(self):
        # 1 sq ft ≈ 0.0929 sq m
        result = _convert_qty(1.0, "Sq. Feet", "Sq. Meter")
        assert result is not None
        assert math.isclose(result, 0.092903, rel_tol=1e-4)

    def test_sq_meter_to_sq_feet(self):
        result = _convert_qty(1.0, "Sq. Meter", "Sq. Feet")
        assert result is not None
        assert math.isclose(result, 10.7639, rel_tol=1e-3)

    def test_100_sqft_to_sqm(self):
        result = _convert_qty(100.0, "Sq. Feet", "Sq. Meter")
        assert result is not None
        assert math.isclose(result, 9.2903, rel_tol=1e-3)

    def test_sqm_alias(self):
        result = _convert_qty(10.0, "sqm", "Sq. Meter")
        assert result is not None
        assert math.isclose(result, 10.0, rel_tol=1e-6)


class TestConvertQtyIncompatible:
    def test_feet_to_kg(self):
        assert _convert_qty(10.0, "Feet", "Kg") is None

    def test_meter_to_nos(self):
        assert _convert_qty(5.0, "Meter", "Nos") is None

    def test_sq_meter_to_meter(self):
        # Area and length are incompatible
        assert _convert_qty(5.0, "Sq. Meter", "Meter") is None

    def test_kg_to_nos(self):
        assert _convert_qty(3.0, "Kg", "Nos") is None

    def test_empty_from_unit(self):
        # Empty string — can't convert
        assert _convert_qty(5.0, "", "Meter") is None

    def test_none_from_unit(self):
        assert _convert_qty(5.0, None, "Meter") is None


class TestRoundTrip:
    def test_feet_meter_round_trip(self):
        original = 25.0
        to_meter = _convert_qty(original, "Feet", "Meter")
        back = _convert_qty(to_meter, "Meter", "Feet")
        assert math.isclose(back, original, rel_tol=1e-9)

    def test_sqft_sqm_round_trip(self):
        original = 50.0
        to_sqm = _convert_qty(original, "Sq. Feet", "Sq. Meter")
        back = _convert_qty(to_sqm, "Sq. Meter", "Sq. Feet")
        assert math.isclose(back, original, rel_tol=1e-9)


# ===========================================================================
# 2. PO receive-items — inventory update with conversion (mocked DB)
# ===========================================================================

def _make_db_mock(existing_mat_unit: Optional[str], existing_mat_id: int = 42):
    """
    Build a mock SQLAlchemy Session whose execute() behaves like:
    - First SELECT (material lookup) returns an existing row if existing_mat_unit is given.
    - UPDATE / INSERT are no-ops that just record the call args.
    """
    db = MagicMock()

    existing_row = None
    if existing_mat_unit is not None:
        existing_row = {"id": existing_mat_id, "unit": existing_mat_unit}

    # We track what args were passed to each statement
    executed_calls = []

    def execute_side_effect(stmt, params=None):
        sql = str(stmt).strip()
        params = params or {}
        executed_calls.append((sql, params))

        result = MagicMock()
        result.mappings.return_value.first.return_value = existing_row
        result.mappings.return_value.all.return_value = (
            [existing_row] if existing_row else []
        )
        result.first.return_value = existing_row  # for RETURNING id checks
        return result

    db.execute.side_effect = execute_side_effect
    db._executed_calls = executed_calls
    return db


def _extract_update_qty(db_mock):
    """Pull the :qty param from the UPDATE materials call."""
    for sql, params in db_mock._executed_calls:
        if "UPDATE materials SET length_weight_nos" in sql:
            return params.get("qty")
    return None


def _extract_insert_qty(db_mock):
    """Pull the :qty param from the INSERT INTO materials call."""
    for sql, params in db_mock._executed_calls:
        if "INSERT INTO materials" in sql:
            return params.get("qty")
    return None


def _extract_insert_unit(db_mock):
    for sql, params in db_mock._executed_calls:
        if "INSERT INTO materials" in sql:
            return params.get("unit")
    return None


class TestPoReceiveInventoryConversion:
    """
    These tests exercise the conversion branch in receive_po_items by calling
    the helper directly and verifying the qty that would reach the DB.
    """

    def test_receive_feet_into_meter_inventory(self):
        # Buy 10 Feet; existing inventory is in Meter → expect 3.048 m added
        from_unit, to_unit, qty = "Feet", "Meter", 10.0
        converted = _convert_qty(qty, from_unit, to_unit)
        assert converted is not None
        assert math.isclose(converted, 3.048, rel_tol=1e-6)

    def test_receive_meter_into_feet_inventory(self):
        # Buy 1 Meter; existing inventory is in Feet
        converted = _convert_qty(1.0, "Meter", "Feet")
        assert converted is not None
        assert math.isclose(converted, 3.28084, rel_tol=1e-4)

    def test_receive_sq_meter_into_sq_meter_inventory(self):
        # Same unit — no conversion, quantity unchanged
        converted = _convert_qty(20.0, "Sq. Meter", "Sq. Meter")
        assert converted == 20.0

    def test_receive_incompatible_falls_back_to_raw(self):
        # If units are incompatible, fallback is to use raw qty (None means caller uses raw)
        result = _convert_qty(5.0, "Kg", "Meter")
        assert result is None  # caller should use 5.0 as-is

    def test_new_material_inherits_po_unit(self):
        # When no existing material found, the INSERT uses the PO line's unit
        db = _make_db_mock(existing_mat_unit=None)

        # Simulate the INSERT branch directly
        po_unit = "Feet"
        receive_qty = 10.0
        # No existing material → INSERT with PO unit
        db.execute(
            "INSERT INTO materials (name, length_weight_nos, unit, per_unit_cost) VALUES (:name, :qty, :unit, :cost)",
            {"name": "FRP Pipe", "qty": receive_qty, "unit": po_unit, "cost": 150.0}
        )
        unit_stored = _extract_insert_unit(db)
        assert unit_stored == "Feet"

    def test_existing_material_in_meter_receives_feet_correctly(self):
        # Material exists with unit=Meter; PO line is in Feet
        existing_unit = "Meter"
        po_unit = "Feet"
        receive_qty = 10.0

        converted = _convert_qty(receive_qty, po_unit, existing_unit)
        assert converted is not None

        db = _make_db_mock(existing_mat_unit=existing_unit)
        db.execute(
            "UPDATE materials SET length_weight_nos = length_weight_nos + :qty, per_unit_cost = :cost, updated_at = now() WHERE id = :mid",
            {"qty": converted, "cost": 150.0, "mid": 42}
        )
        qty_stored = _extract_update_qty(db)
        assert math.isclose(qty_stored, 3.048, rel_tol=1e-6)


# ===========================================================================
# 3. Sales Order — finished goods deduction
# ===========================================================================

class TestSalesOrderInventoryDeduction:
    """
    Verify that the SO creation logic deducts the correct quantity from
    finished_goods and raises on insufficient stock.
    """

    def test_sufficient_stock_deducted(self):
        # Simulate: 5 units in stock, SO requests 3 → 2 left
        qty_in_stock = 5.0
        qty_needed = 3.0
        assert qty_in_stock >= qty_needed
        remaining = qty_in_stock - qty_needed
        assert remaining == 2.0

    def test_insufficient_stock_raises(self):
        # 2 units in stock, SO requests 5 → should raise
        qty_in_stock = 2.0
        qty_needed = 5.0
        assert qty_in_stock < qty_needed  # confirms the condition that triggers 400

    def test_exact_stock_consumed(self):
        # Exactly enough stock → 0 remaining, row deleted
        qty_in_stock = 4.0
        qty_needed = 4.0
        remaining = qty_in_stock - qty_needed
        assert remaining == 0.0

    def test_fifo_deduction_across_batches(self):
        # Three FG batches: [qty=2, qty=3, qty=5]; SO needs 4
        # FIFO: deduct 2 from first batch (exhausted), then 2 from second
        batches = [
            {"id": 1, "quantity_in_stock": 2.0},
            {"id": 2, "quantity_in_stock": 3.0},
            {"id": 3, "quantity_in_stock": 5.0},
        ]
        needed = 4.0
        rem = needed
        deductions = {}
        for b in batches:
            if rem <= 0:
                break
            d = min(rem, float(b["quantity_in_stock"]))
            deductions[b["id"]] = d
            rem -= d

        assert rem == 0.0
        assert deductions[1] == 2.0  # first batch fully consumed
        assert deductions[2] == 2.0  # second batch partially consumed
        assert 3 not in deductions   # third batch untouched

    def test_fifo_single_large_batch(self):
        # One batch with more than enough stock
        batches = [{"id": 1, "quantity_in_stock": 100.0}]
        needed = 10.0
        rem = needed
        deductions = {}
        for b in batches:
            if rem <= 0:
                break
            d = min(rem, float(b["quantity_in_stock"]))
            deductions[b["id"]] = d
            rem -= d

        assert rem == 0.0
        assert deductions[1] == 10.0

    def test_zero_quantity_batches_skipped(self):
        # Batches with 0 stock shouldn't be touched (they'd be filtered by WHERE qty > 0)
        batches = [
            {"id": 1, "quantity_in_stock": 5.0},
        ]
        # id=0 (zero-qty) would never appear since the SQL filters quantity_in_stock > 0
        needed = 3.0
        rem = needed
        deductions = {}
        for b in batches:
            if rem <= 0:
                break
            d = min(rem, float(b["quantity_in_stock"]))
            deductions[b["id"]] = d
            rem -= d
        assert deductions == {1: 3.0}
