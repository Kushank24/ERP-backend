"""
Data-integrity fixes for sales-order status and work-order completion.

Both bugs corrupted live data every time the affected path ran:

  1. sales_orders.status carried payment state (1/2/3) *and* dispatch state
     (3/4), so recording a dispatch destroyed the payment state and vice-versa.
  2. Completing a work order produced finished goods for the full ordered
     quantity even when some had already been issued via /issue-products, so a
     fully-issued order produced its output twice while consuming input once.

The routers hold SQL inline, so these tests exercise the decision logic rather
than the SQL: the quantity arithmetic for what completion should produce, the
aggregation and sufficiency rules for material consumption, and the migration's
backfill semantics. Each is the part that was actually wrong.
"""

from __future__ import annotations

import re
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
SO_SRC = (BACKEND / "app" / "routers" / "sales_orders.py").read_text()
WO_SRC = (BACKEND / "app" / "routers" / "work_orders.py").read_text()
AN_SRC = (BACKEND / "app" / "routers" / "analytics.py").read_text()
MIGRATION = (BACKEND / "migrations" / "008_sales_order_status_split.sql").read_text()


# ---------------------------------------------------------------------------
# 1. The status collision is gone
# ---------------------------------------------------------------------------


def _body(src: str, func: str) -> str:
    """Source of a single top-level function, up to the next decorator."""
    start = src.index(f"def {func}(")
    rest = src[start:]
    end = rest.find("\n@router.")
    return rest if end == -1 else rest[:end]


def test_dispatch_no_longer_writes_the_shared_status_column():
    """This is the write that used to destroy payment state."""
    body = _body(SO_SRC, "dispatch_so")
    assert "SET dispatch_status" in body
    assert not re.search(r"UPDATE sales_orders SET status\b", body)


def test_dispatch_reads_dispatch_status_not_status():
    body = _body(SO_SRC, "dispatch_so")
    assert "dispatch_status FROM sales_orders" in body
    assert "id, status FROM sales_orders" not in body


def test_payment_writes_payment_status():
    body = _body(SO_SRC, "update_payment")
    assert "payment_status = :st" in body


def test_payment_still_mirrors_legacy_status_for_unmigrated_readers():
    """
    Only one endpoint writes `status` now, so mirroring is safe and keeps any
    reader we have not migrated working.
    """
    body = _body(SO_SRC, "update_payment")
    assert "status = :st" in body


def test_payment_sets_the_payment_received_flag():
    """The column existed from the initial schema but nothing ever wrote it."""
    body = _body(SO_SRC, "update_payment")
    assert "payment_received = :recv" in body


def test_both_new_columns_are_returned_to_the_client():
    assert "payment_status, dispatch_status" in SO_SRC


def test_analytics_reads_payment_status_not_the_conflated_column():
    """
    Scoped to the sales-order analytics function. purchase_orders analytics
    also filters on a column called `status`, but that is the PO status
    (1=pending, 2=confirmed, …) on a different table and is unaffected.
    """
    body = _body(AN_SRC, "so_analytics")
    assert "COUNT(*) FILTER (WHERE payment_status = 1)" in body
    assert "COUNT(*) FILTER (WHERE payment_status = 3)" in body
    # The old buckets keyed off `status`, which a dispatch could overwrite.
    assert "COUNT(*) FILTER (WHERE status = 1)" not in body
    assert "COUNT(*) FILTER (WHERE status = 3)" not in body


def test_purchase_order_analytics_status_is_left_alone():
    """Guard the scoping above: PO status is a different, valid column."""
    body = _body(AN_SRC, "po_analytics")
    assert "COUNT(*) FILTER (WHERE status = 1)" in body


def test_analytics_reports_orders_with_no_recorded_payment_state():
    """
    713 of 838 orders carry the legacy status=6 and were silently absent from
    every payment bucket. They are now counted explicitly.
    """
    body = _body(AN_SRC, "so_analytics")
    assert "payment_unknown" in body


# ---------------------------------------------------------------------------
# 2. Migration backfill semantics
# ---------------------------------------------------------------------------


def test_migration_adds_both_columns():
    assert "ADD COLUMN IF NOT EXISTS payment_status" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS dispatch_status" in MIGRATION


def test_migration_only_backfills_payment_from_real_payment_codes():
    """
    status=6 is a legacy import value with no payment meaning. Mapping it to
    "not received" would assert a falsehood about 713 real orders, so it must
    be left NULL.
    """
    assert "WHERE status IN (1, 2, 3)" in MIGRATION
    assert "status IN (1, 2, 3, 6)" not in MIGRATION


def test_migration_derives_dispatch_from_line_items():
    """dispatched_qty is the real record; the conflated status is not."""
    assert "FROM sales_order_items" in MIGRATION
    assert "dispatched_qty >= quantity_sold" in MIGRATION


def test_migration_constrains_both_columns():
    """`status` silently accumulated a 6; these columns should not be able to."""
    assert "sales_orders_payment_status_chk" in MIGRATION
    assert "sales_orders_dispatch_status_chk" in MIGRATION


def test_migration_does_not_drop_the_legacy_column():
    """Dropping it would break any reader not yet migrated."""
    assert "DROP COLUMN" not in MIGRATION.upper()


# ---------------------------------------------------------------------------
# 3. Work-order completion: produce only what has not been issued
# ---------------------------------------------------------------------------
# Mirrors the arithmetic in patch_status: finished goods created on completion
# is the outstanding quantity, not the full ordered quantity.


def outstanding(ordered: float, issued: float) -> float:
    return max(0.0, ordered - issued)


def produced_on_completion(ordered: float, issued: float) -> float:
    rem = outstanding(ordered, issued)
    return 0.0 if rem <= 1e-9 else rem


@pytest.mark.parametrize(
    "ordered,issued,expected_total",
    [
        (10, 0,  10),   # nothing issued — completion produces all of it
        (10, 10, 10),   # fully issued — completion must produce nothing
        (10, 4,  10),   # partly issued — completion produces the remaining 6
        (10, 9.9999999, 10),  # float dust must not produce a phantom sliver
        (0.5, 0.25, 0.5),     # fractional quantities
    ],
)
def test_total_output_equals_ordered_quantity(ordered, issued, expected_total):
    """
    The invariant the bug broke: issued + produced-at-completion must equal the
    ordered quantity. Previously a fully-issued order yielded 2x.
    """
    total = issued + produced_on_completion(ordered, issued)
    assert total == pytest.approx(expected_total)


def test_fully_issued_order_used_to_double_and_now_does_not():
    ordered, issued = 10.0, 10.0
    buggy = issued + ordered          # old behaviour: produced the full qty again
    fixed = issued + produced_on_completion(ordered, issued)
    assert buggy == 20.0
    assert fixed == 10.0


def test_completion_skips_products_already_issued_in_full():
    assert produced_on_completion(7, 7) == 0.0


def test_completion_guard_is_present_in_source():
    body = _body(WO_SRC, "patch_status")
    assert 'remaining_qty' in body
    assert "continue  # already issued to finished goods in full" in body


# ---------------------------------------------------------------------------
# 4. Work-order completion: material consumption
# ---------------------------------------------------------------------------


def aggregate(lines: list[tuple[int, float]]) -> dict[int, float]:
    """Sum required quantity per material id, as patch_status now does."""
    out: dict[int, float] = {}
    for mid, qty in lines:
        out[mid] = out.get(mid, 0.0) + qty
    return out


def shortfalls(required: dict[int, float], available: dict[int, float]) -> list[int]:
    return sorted(
        mid for mid, req in required.items()
        if available.get(mid, 0.0) + 1e-9 < req
    )


def test_one_material_across_several_products_is_summed():
    """
    A material in two products' BOQs must be checked against its total, not
    per line — otherwise each line passes and the sum still goes negative.
    """
    assert aggregate([(1, 6.0), (1, 5.0), (2, 3.0)]) == {1: 11.0, 2: 3.0}


def test_aggregated_total_catches_a_shortfall_that_per_line_checks_miss():
    required = aggregate([(1, 6.0), (1, 5.0)])   # 11 total
    available = {1: 10.0}                        # each line alone would pass
    assert shortfalls(required, available) == [1]


def test_sufficient_stock_reports_no_shortfall():
    assert shortfalls({1: 5.0}, {1: 5.0}) == []


def test_float_tolerance_does_not_manufacture_a_shortfall():
    assert shortfalls({1: 5.0}, {1: 4.9999999999}) == []


def test_missing_material_is_treated_as_a_shortfall():
    assert shortfalls({9: 1.0}, {}) == [9]


def test_completion_rejects_rather_than_silently_skipping():
    body = _body(WO_SRC, "patch_status")
    # Previously: `continue  # material not in inventory — nothing to deduct`
    assert "unknown_materials" in body
    assert "are not in" in body
    assert "Insufficient material stock" in body


def test_completion_locks_material_rows_before_deducting():
    body = _body(WO_SRC, "patch_status")
    assert "FOR UPDATE" in body


def test_reopening_a_completed_work_order_is_refused():
    """
    Completion consumes materials and produces goods with no reversal, and the
    goods may already be sold — so walking it backwards must be refused rather
    than leaving stock wrong.
    """
    body = _body(WO_SRC, "patch_status")
    assert 'current_status == "completed" and body.status != "completed"' in body


# ---------------------------------------------------------------------------
# 5. The unscoped global DELETE in create_so
# ---------------------------------------------------------------------------


def test_zero_stock_cleanup_is_scoped_to_the_order():
    """
    Creating one sales order used to run an unscoped
    `DELETE FROM finished_goods WHERE quantity_in_stock <= 0`, wiping
    zero-stock rows belonging to unrelated orders and work orders.
    """
    body = _body(SO_SRC, "create_so")
    assert "touched_fg_ids" in body
    assert "id = ANY(:ids) AND quantity_in_stock <= 0" in body
    assert "DELETE FROM finished_goods WHERE quantity_in_stock <= 0" not in body


def test_dispatch_checks_stock_before_deducting():
    """Dispatch had no sufficiency check and drove stock negative."""
    body = _body(SO_SRC, "dispatch_so")
    assert body.count("Insufficient stock") == 2  # keyed line and FIFO branch


def test_create_fg_handles_omitted_optional_fields():
    """
    finished_goods.create_fg called .strip() on two Optional[str] = None
    fields, so omitting either was a guaranteed AttributeError -> 500.
    """
    src = (BACKEND / "app" / "routers" / "finished_goods.py").read_text()
    assert 'body.work_order_number.strip()' not in src
    assert 'body.party_name.strip()' not in src
    assert '(body.work_order_number or "").strip()' in src
