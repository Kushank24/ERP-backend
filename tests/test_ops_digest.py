"""
Daily internal ops digest.

Three sections, all standing recurring lists — no dedup, no one-time trigger:
  - Open work orders: every work order currently status='in-progress'.
  - Unpaid sales orders: every order with payment_status IN (1, 2) — a real,
    assessed "Not Received"/"Partially Received" status. Deliberately
    excludes payment_status IS NULL: 713 orders from the original bulk
    import were never given a real payment status at all, and including
    them would turn this section into 760 rows of mostly noise instead of
    ~47 rows of real signal. (This section originally ran as an exact-day
    trigger, matching the offer-reminder pattern, but was changed to a
    standing list to show every currently-unpaid order, not just the ones
    that happened to turn 10 days old today.)
  - Upcoming deliveries: orders with delivery_date in the next N days,
    regardless of dispatch_status — the same order is expected to appear on
    consecutive days as its delivery date approaches, which is intentional.

These tests pin the exclusion of NULL payment_status (the one subtlety in an
otherwise "just list everything" design) and the standing-list nature of all
three sections.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.jobs.ops_digest import (
    DELIVERY_WINDOW_DAYS,
    DIGEST_RECIPIENT,
    UNPAID_PAYMENT_STATUSES,
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


def test_unpaid_statuses_exclude_received_and_exclude_null():
    """
    (1, 2) = Not Received, Partially Received. 3 (Received) and NULL (legacy,
    never assessed) must both stay excluded — NULL is the one that matters:
    including it reopens the 713-row backlog this design excludes on purpose.
    """
    assert set(UNPAID_PAYMENT_STATUSES) == {1, 2}
    assert 3 not in UNPAID_PAYMENT_STATUSES


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
    assert "No unpaid sales orders." in html
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
    """
    payment_status can, in principle, be legacy-NULL if a caller ever
    forgets the WHERE filter — must not crash or show a raw None either way.
    """
    _, html = _build_digest([], [_unpaid(status=None)], [])
    assert "Unknown" in html


def test_delivery_section_shows_dispatch_status_label():
    _, html = _build_digest([], [], [_delivery(dispatch=4)])
    assert "Fully Dispatched" in html


def test_delivery_section_does_not_show_the_order_amount():
    """
    Explicit design decision: unpaid orders show amount (relevant to
    collections); upcoming deliveries do not (it's a logistics list, not a
    money one). Guards against the column silently coming back.
    """
    _, html = _build_digest([], [], [_delivery(amt=123456.78)])
    delivery_section = html.split("3. Upcoming")[-1]
    assert "₹" not in delivery_section


def test_delivery_section_includes_already_dispatched_orders():
    """
    Explicit design decision: unlike the unpaid-orders section, this is a pure
    calendar heads-up. All 6 real matches on the day this was built were
    already dispatch_status=4, and the decision was to show them anyway.
    """
    _, html = _build_digest([], [], [_delivery(inv="INV/999", dispatch=4)])
    assert "INV/999" in html


def test_section_counts_reflect_actual_row_counts():
    _, html = _build_digest([_wo(), _wo(id_=2)], [_unpaid(), _unpaid(id_=2), _unpaid(id_=3)], [])
    assert "Open Work Orders (2)" in html
    assert "Unpaid Sales Orders (3)" in html


def test_unpaid_section_header_has_no_age_qualifier():
    """
    Pinned against reintroducing the exact-day wording — this is a standing
    list now, so the header must not claim anything about age.
    """
    _, html = _build_digest([], [_unpaid()], [])
    assert "days old" not in html


def test_company_and_party_names_are_html_escaped():
    _, html = _build_digest([_wo(party="A & B Co")], [], [])
    assert "A &amp; B Co" in html


# ---------------------------------------------------------------------------
# run(): dry-run vs live — no marking behaviour anymore (standing list)
# ---------------------------------------------------------------------------


def _mock_db_with(wo_rows, unpaid_rows, delivery_rows):
    db = MagicMock()

    def execute_side_effect(query, params=None):
        result = MagicMock()
        sql = str(query)
        if "work_orders" in sql:
            result.mappings.return_value.all.return_value = wo_rows
        elif "sales_orders" in sql and "payment_status = ANY" in sql:
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
def test_successful_send_returns_zero(mock_send, mock_session):
    db = _mock_db_with([], [_unpaid(id_=7), _unpaid(id_=8)], [])
    mock_session.return_value.__enter__.return_value = db

    exit_code = run(dry_run=False)

    assert exit_code == 0
    mock_send.assert_called_once()


@patch("app.jobs.ops_digest.SessionLocal")
@patch("app.jobs.ops_digest.send_transactional_email")
def test_failed_send_returns_nonzero(mock_send, mock_session):
    db = _mock_db_with([], [_unpaid(id_=7)], [])
    mock_session.return_value.__enter__.return_value = db
    mock_send.side_effect = RuntimeError("Resend rejected message")

    exit_code = run(dry_run=False)

    assert exit_code == 1


@patch("app.jobs.ops_digest.SessionLocal")
@patch("app.jobs.ops_digest.send_transactional_email")
def test_unpaid_orders_are_never_written_back_to_the_db(mock_send, mock_session):
    """
    This section has no dedup/mark-sent state anymore — it's a plain
    standing list, like open work orders. run() must not issue any UPDATE.
    """
    db = _mock_db_with([_wo()], [_unpaid(id_=7)], [_delivery()])
    mock_session.return_value.__enter__.return_value = db

    run(dry_run=False)

    update_calls = [c for c in db.execute.call_args_list if "UPDATE" in str(c.args[0])]
    assert update_calls == []


@patch("app.jobs.ops_digest.SessionLocal")
@patch("app.jobs.ops_digest.send_transactional_email")
def test_send_is_addressed_to_accounts_mailbox(mock_send, mock_session):
    db = _mock_db_with([], [], [])
    mock_session.return_value.__enter__.return_value = db

    run(dry_run=False)

    args, _ = mock_send.call_args
    assert args[0] == DIGEST_RECIPIENT
