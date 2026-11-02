"""
Daily internal operations digest — one email to accounts@esafe.co.in with
three sections:

    1. Open work orders       — every work order currently status='in-progress'.
                                 A standing daily list, not a one-time alert:
                                 the same work order appears every day until
                                 it's marked completed. Only 25 exist at the
                                 time this was built, so this is a genuine
                                 "what's still open" digest, not backlog spam.

    2. Unpaid sales orders    — every order with payment_status IN (1, 2)
                                 ("Not Received" / "Partially Received"), a
                                 standing list like open work orders, not a
                                 one-time alert. The money column is what is
                                 still *owed*, not the order value: for a
                                 Partially Received order that is
                                 total_amount - payment_amount, since the
                                 part-payment is recorded. Deliberately
                                 excludes orders where payment_status IS NULL:
                                 713 orders from the original bulk import were
                                 never given a real payment status at all, and
                                 are not the same thing as an order someone
                                 actually assessed as unpaid — including them
                                 would make this section 760 rows of mostly
                                 noise instead of 47 rows of real signal. This
                                 originally ran as an exact-day trigger (see
                                 migration 010 / offers.reminder_sent_at for
                                 that pattern) but was changed to a standing
                                 list; payment_reminder_sent_at is no longer
                                 written by this job.

    3. Upcoming deliveries    — work orders with delivery_date within the next
                                 5 days (today through today+5 inclusive),
                                 each with whether the work order is completed
                                 or still in progress. Driven by the work
                                 order rather than the sales order because the
                                 work order is where the promised delivery
                                 date is actually maintained (1022 of 1034
                                 work orders carry one), and because what the
                                 team needs to see is whether the thing due to
                                 go out is actually finished.

                                 Deliberately recurring, not deduplicated: the
                                 same work order is expected to appear on
                                 consecutive days as its delivery date
                                 approaches, which is useful planning
                                 information rather than spam. Completed work
                                 orders are listed too — "due Thursday, and
                                 done" is exactly as much a planning fact as
                                 "due Thursday, not started", and hiding them
                                 would make a short list look alarming.
                                 Expect overlap with section 1: an in-progress
                                 work order due this week appears in both, by
                                 design — section 1 is "what is open", this is
                                 "what is due".

Run standalone:

    python -m app.jobs.ops_digest
    python -m app.jobs.ops_digest --dry-run

In production this is not run directly — app/jobs/daily_notifications.py
runs this together with the offer reminder job as a single scheduled command.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from html import escape

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..email_service import send_transactional_email
from ..logging_config import configure_logging

logger = logging.getLogger(__name__)

DELIVERY_WINDOW_DAYS = 5
DIGEST_RECIPIENT = "accounts@esafe.co.in"

#: Real, assessed payment states only. Deliberately excludes NULL — see the
#: module docstring for why (713 legacy bulk-import rows with no real status).
UNPAID_PAYMENT_STATUSES = (1, 2)

PAYMENT_STATUS_LABEL = {1: "Not Received", 2: "Partially Received", 3: "Received"}

#: payment_status 2 = Partially Received, the one state where part of the money
#: is already in and the outstanding figure differs from the order total.
PARTIALLY_RECEIVED = 2

WORK_ORDER_STATUS_LABEL = {"completed": "Completed", "in-progress": "In Progress"}


_OPEN_WORK_ORDERS_SQL = text(
    """
    SELECT id, work_order_number, party_name, creation_date
    FROM work_orders
    WHERE status = 'in-progress'
    ORDER BY creation_date ASC
    """
)

_UNPAID_SALES_ORDERS_SQL = text(
    """
    SELECT id, invoice_number, company_name, sales_date, total_amount, payment_status,
           payment_amount
    FROM sales_orders
    WHERE payment_status = ANY(:statuses)
    ORDER BY sales_date ASC
    """
)

#: Driven by work_orders.delivery_date, not sales_orders.delivery_date — see
#: section 3 of the module docstring.
_UPCOMING_DELIVERIES_SQL = text(
    """
    SELECT id, work_order_number, party_name, delivery_date, status
    FROM work_orders
    WHERE delivery_date BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '1 day' * :window_days
    ORDER BY delivery_date ASC, work_order_number ASC
    """
)


def _fmt_money(amount: float) -> str:
    return f"₹{amount:,.2f}"


def _outstanding(order: dict) -> float:
    """
    What is still owed on an unpaid sales order.

    For "Not Received" that is the whole order. For "Partially Received" the
    part-payment is recorded in payment_amount, so the outstanding figure is
    the remainder — what someone chasing the payment actually needs to ask for.

    Defensive on two counts: a Partially Received order with no payment_amount
    recorded falls back to the full total (nothing is known to have come in, so
    claiming it is paid off would be the worse error), and the result is never
    negative. Neither case exists in the data today — the payment endpoint
    rejects an amount above the order total — but this runs unattended against
    a table with legacy rows in it.
    """
    total = float(order.get("total_amount") or 0)
    if order.get("payment_status") != PARTIALLY_RECEIVED:
        return total
    received = order.get("payment_amount")
    if received is None:
        return total
    return max(total - float(received), 0.0)


def _outstanding_cell(order: dict) -> str:
    """
    The Outstanding cell. On a part-paid order the order value is shown beside
    the remainder, greyed out — without it a row reading "₹28,879.00" against a
    ₹9.4 lakh invoice looks like a data error rather than a part-payment.
    """
    outstanding = _fmt_money(_outstanding(order))
    if order.get("payment_status") != PARTIALLY_RECEIVED:
        return escape(outstanding)
    total = _fmt_money(float(order.get("total_amount") or 0))
    return (
        f"{escape(outstanding)} "
        f"<span style='color:#888'>of {escape(total)}</span>"
    )


def _fmt_date(d) -> str:
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d")
    return d.strftime("%d %b %Y")


def _table(headers: list[str], rows: list[list[str]], empty_message: str) -> str:
    if not rows:
        return f"<p style='color:#666;font-size:13px;margin:4px 0 0'>{escape(empty_message)}</p>"
    head = "".join(
        f"<th style='text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid #ccc'>{escape(h)}</th>"
        for h in headers
    )
    body = "".join(
        "<tr>" + "".join(f"<td style='padding:4px 12px 4px 0'>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table style='font-size:13px;margin:6px 0 0;width:100%'><tr>{head}</tr>{body}</table>"


def _fetch_open_work_orders(db: Session) -> list[dict]:
    rows = db.execute(_OPEN_WORK_ORDERS_SQL).mappings().all()
    return [dict(r) for r in rows]


def _fetch_unpaid_sales_orders(db: Session) -> list[dict]:
    rows = db.execute(
        _UNPAID_SALES_ORDERS_SQL, {"statuses": list(UNPAID_PAYMENT_STATUSES)}
    ).mappings().all()
    return [dict(r) for r in rows]


def _fetch_upcoming_deliveries(db: Session) -> list[dict]:
    rows = db.execute(_UPCOMING_DELIVERIES_SQL, {"window_days": DELIVERY_WINDOW_DAYS}).mappings().all()
    return [dict(r) for r in rows]


def _build_digest(
    work_orders: list[dict], unpaid: list[dict], deliveries: list[dict]
) -> tuple[str, str]:
    today_str = datetime.now().strftime("%d %b %Y")
    subject = f"Daily Ops Digest — {today_str}"

    wo_table = _table(
        ["Work Order", "Party", "Created"],
        [
            [
                escape(str(w["work_order_number"])),
                escape(str(w["party_name"] or "—")),
                _fmt_date(w["creation_date"]),
            ]
            for w in work_orders
        ],
        "No work orders currently open.",
    )

    unpaid_table = _table(
        ["Invoice", "Company", "Order Date", "Outstanding", "Payment Status"],
        [
            [
                escape(str(u["invoice_number"])),
                escape(str(u["company_name"] or "—")),
                _fmt_date(u["sales_date"]),
                _outstanding_cell(u),
                escape(PAYMENT_STATUS_LABEL.get(u["payment_status"], "Unknown")),
            ]
            for u in unpaid
        ],
        "No unpaid sales orders.",
    )

    delivery_table = _table(
        ["Work Order", "Party", "Delivery Date", "Status"],
        [
            [
                escape(str(d["work_order_number"])),
                escape(str(d["party_name"] or "—")),
                _fmt_date(d["delivery_date"]),
                escape(WORK_ORDER_STATUS_LABEL.get(d["status"], str(d["status"] or "Unknown"))),
            ]
            for d in deliveries
        ],
        f"No work orders due for delivery in the next {DELIVERY_WINDOW_DAYS} days.",
    )

    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#222">
      <p><strong>Daily Ops Digest — {today_str}</strong></p>

      <p style="margin:16px 0 2px"><strong>1. Open Work Orders ({len(work_orders)})</strong></p>
      {wo_table}

      <p style="margin:16px 0 2px"><strong>2. Unpaid Sales Orders ({len(unpaid)})</strong></p>
      {unpaid_table}

      <p style="margin:16px 0 2px"><strong>3. Upcoming Deliveries — next {DELIVERY_WINDOW_DAYS} days ({len(deliveries)})</strong></p>
      {delivery_table}

      <p style="margin-top:20px;color:#666;font-size:12px">
        Automated internal digest — E-SAFE Enterprises ERP.
      </p>
    </div>
    """.strip()

    return subject, html


def run(dry_run: bool) -> int:
    """Returns the process exit code."""
    with SessionLocal() as db:
        work_orders = _fetch_open_work_orders(db)
        unpaid = _fetch_unpaid_sales_orders(db)
        deliveries = _fetch_upcoming_deliveries(db)

        logger.info(
            "Ops digest: %d open work order(s), %d unpaid sales order(s) "
            "(%s outstanding), %d work order(s) due for delivery within %d days",
            len(work_orders),
            len(unpaid),
            _fmt_money(sum(_outstanding(u) for u in unpaid)),
            len(deliveries),
            DELIVERY_WINDOW_DAYS,
        )

        subject, html = _build_digest(work_orders, unpaid, deliveries)

        if dry_run:
            logger.info("[DRY RUN] Would send ops digest to %s", DIGEST_RECIPIENT)
            return 0

        try:
            send_transactional_email(DIGEST_RECIPIENT, subject, html)
        except Exception as exc:
            logger.error("Ops digest failed to send to %s: %s", DIGEST_RECIPIENT, exc)
            return 1

        logger.info("Ops digest sent to %s", DIGEST_RECIPIENT)
        return 0


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be sent without sending or marking anything.",
    )
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
