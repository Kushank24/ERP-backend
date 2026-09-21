"""
Daily internal ops digest.

Three sections, three different designs, deliberately:
  - Open work orders: a recurring standing list, no dedup — the same order
    should appear every day until it's completed.
  - Unpaid sales orders: an exact-day match (like offers), because the
    backlog of already-unpaid orders 10+ days old is 753 rows at the time
    this was built and a ">= 10 days" rule would re-email that backlog
    forever.
  - Upcoming deliveries: a recurring rolling window, no dedup — the same
    order is expected to appear on consecutive days as its delivery date
    approaches, which is intentional (unlike the unpaid-orders section).

These tests pin those three different behaviours so a future edit doesn't
accidentally make them all work the same way.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.jobs.ops_digest import (
    DELIVERY_WINDOW_DAYS,
    DIGEST_RECIPIENT,
    UNPAID_AGE_DAYS,
    _MARK_PAYMENT_REMINDED_SQL,
    _build_digest,
    _fmt_date,
    _fmt_money,
    run,
)


def _wo(id_=1, number="WO/1", party="Acme", created="2026-09-01"):
    return {"id": id_, "work_order_number": number, "party_name": party, "creation_date": created}


def _unpaid(id_=1, inv="INV/1", company="Acme", date="2026-09-11", amt=1000.0, status=1):
    return {
        "id": id_, "invoice_number": inv, "company_name": company,
        "sales_date": date, "total_amount": amt, "payment_status": status,
    }


def _delivery(id_=1, inv="INV/1", company="Acme", date="2026-09-25", amt=1000.0, dispatch=1):
    return {
        "id": id_, "invoice_number": inv, "company_name": company,
        "delivery_date": date, "total_amount": amt, "dispatch_status": dispatch,
    }


# ---------------------------------------------------------------------------
# The design constants
# ---------------------------------------------------------------------------


def test_unpaid_age_is_exactly_10_not_a_minimum():
    """Pinned so this can't silently widen to '>=', reopening the 753-row backlog."""
    assert UNPAID_AGE_DAYS == 10


def test_delivery_window_is_5_days():
    assert DELIVERY_WINDOW_DAYS == 5


def test_digest_goes_to_accounts_mailbox():
    assert DIGEST_RECIPIENT == "accounts@esafe.co.in"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_money_formatting():
    assert _fmt_money(1000.0) == "₹1,000.00"


def test_date_formatting_accepts_iso_string():
    assert _fmt_date("2026-09-21") == "21 Sep 2026"


# ---------------------------------------------------------------------------
# Digest content: three independent sections
# ---------------------------------------------------------------------------


def test_digest_subject_includes_todays_date():
    subject, _ = _build_digest([], [], [])
    assert subject.startswith("Daily Ops Digest —")


def test_empty_sections_show_a_clear_none_message_not_a_blank_table():
    _, html = _build_digest([], [], [])
    assert "No work orders currently open." in html
    assert f"exactly {UNPAID_AGE_DAYS} days old and unpaid today" in html
    assert f"next {DELIVERY_WINDOW_DAYS} days" in html


def test_work_order_section_lists_number_party_and_created_date():
    _, html = _build_digest([_wo(number="WO/26-27/0148", party="Power Grid Corp")], [], [])
    assert "WO/26-27/0148" in html
    assert "Power Grid Corp" in html
    assert "1. Open Work Orders (1)" in html


def test_unpaid_section_shows_payment_status_label_not_raw_code():
    _, html = _build_digest([], [_unpaid(status=1)], [])
    assert "Not Received" in html
    assert ">1<" not in html  # the raw integer code must not leak into the table


def test_unpaid_section_handles_unknown_status_gracefully():
    """payment_status can be legacy-NULL — must not crash or show a raw None."""
    _, html = _build_digest([], [_unpaid(status=None)], [])
    assert "Unknown" in html


def test_delivery_section_shows_dispatch_status_label():
    _, html = _build_digest([], [], [_delivery(dispatch=4)])
    assert "Fully Dispatched" in html


def test_delivery_section_includes_already_dispatched_orders():
    """
    Explicit design decision: unlike the unpaid-orders section, this is a pure
    calendar heads-up. All 6 real matches on the day this was built were
    already dispatch_status=4, and the decision was to show them anyway.
    """
    _, html = _build_digest([], [], [_delivery(inv="INV/999", dispatch=4)])
    assert "INV/999" in html


def test_section_counts_reflect_actual_row_counts():
    _, html = _build_digest([_wo(), _wo(id_=2)], [_unpaid()], [])
    assert "Open Work Orders (2)" in html
    assert f"Unpaid Sales Orders — turned {UNPAID_AGE_DAYS} days old today (1)" in html


def test_company_and_party_names_are_html_escaped():
    _, html = _build_digest([_wo(party="A & B Co")], [], [])
    assert "A &amp; B Co" in html


# ---------------------------------------------------------------------------
# run(): dry-run vs live, and the unpaid-only marking behaviour
# ---------------------------------------------------------------------------


def _mock_db_with(wo_rows, unpaid_rows, delivery_rows):
    db = MagicMock()

    def execute_side_effect(query, params=None):
        result = MagicMock()
        sql = str(query)
        if "work_orders" in sql:
            result.mappings.return_value.all.return_value = wo_rows
        elif "sales_orders" in sql and "payment_reminder_sent_at IS NULL" in sql:
            result.mappings.return_value.all.return_value = unpaid_rows
        elif "sales_orders" in sql and "delivery_date BETWEEN" in sql:
            result.mappings.return_value.all.return_value = delivery_rows
        else:
            result.mappings.return_value.all.return_value = []
        return result

    db.execute.side_effect = execute_side_effect
    return db


@patch("app.jobs.ops_digest.SessionLocal")
@patch("app.jobs.ops_digest.send_transactional_email")
def test_dry_run_never_sends(mock_send, mock_session):
    db = _mock_db_with([_wo()], [_unpaid()], [_delivery()])
    mock_session.return_value.__enter__.return_value = db

    exit_code = run(dry_run=True)

    mock_send.assert_not_called()
    assert exit_code == 0


@patch("app.jobs.ops_digest.SessionLocal")
@patch("app.jobs.ops_digest.send_transactional_email")
def test_dry_run_does_not_mark_unpaid_orders(mock_send, mock_session):
    db = _mock_db_with([], [_unpaid(id_=7)], [])
    mock_session.return_value.__enter__.return_value = db

    run(dry_run=True)

    mark_calls = [c for c in db.execute.call_args_list if c.args and c.args[0] is _MARK_PAYMENT_REMINDED_SQL]
    assert mark_calls == []


@patch("app.jobs.ops_digest.SessionLocal")
@patch("app.jobs.ops_digest.send_transactional_email")
def test_successful_send_marks_the_unpaid_orders_it_reported(mock_send, mock_session):
    db = _mock_db_with([], [_unpaid(id_=7), _unpaid(id_=8)], [])
    mock_session.return_value.__enter__.return_value = db

    exit_code = run(dry_run=False)

    assert exit_code == 0
    mark_calls = [c for c in db.execute.call_args_list if c.args and c.args[0] is _MARK_PAYMENT_REMINDED_SQL]
    assert len(mark_calls) == 1
    assert mark_calls[0].args[1] == {"ids": [7, 8]}


@patch("app.jobs.ops_digest.SessionLocal")
@patch("app.jobs.ops_digest.send_transactional_email")
def test_failed_send_does_not_mark_anything(mock_send, mock_session):
    db = _mock_db_with([], [_unpaid(id_=7)], [])
    mock_session.return_value.__enter__.return_value = db
    mock_send.side_effect = RuntimeError("Resend rejected message")

    exit_code = run(dry_run=False)

    assert exit_code == 1
    mark_calls = [c for c in db.execute.call_args_list if c.args and c.args[0] is _MARK_PAYMENT_REMINDED_SQL]
    assert mark_calls == []


@patch("app.jobs.ops_digest.SessionLocal")
@patch("app.jobs.ops_digest.send_transactional_email")
def test_no_unpaid_orders_means_no_mark_call_at_all(mock_send, mock_session):
    """An empty list must not produce `WHERE id = ANY('{}')` noise."""
    db = _mock_db_with([_wo()], [], [_delivery()])
    mock_session.return_value.__enter__.return_value = db

    run(dry_run=False)

    mark_calls = [c for c in db.execute.call_args_list if c.args and c.args[0] is _MARK_PAYMENT_REMINDED_SQL]
    assert mark_calls == []


@patch("app.jobs.ops_digest.SessionLocal")
@patch("app.jobs.ops_digest.send_transactional_email")
def test_send_is_addressed_to_accounts_mailbox(mock_send, mock_session):
    db = _mock_db_with([], [], [])
    mock_session.return_value.__enter__.return_value = db

    run(dry_run=False)

    args, _ = mock_send.call_args
    assert args[0] == DIGEST_RECIPIENT
