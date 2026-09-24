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
                                 one-time alert. Deliberately excludes orders
                                 where payment_status IS NULL: 713 orders from
                                 the original bulk import were never given a
                                 real payment status at all, and are not the
                                 same thing as an order someone actually
                                 assessed as unpaid — including them would
                                 make this section 760 rows of mostly noise
                                 instead of 47 rows of real signal. This
                                 originally ran as an exact-day trigger (see
                                 migration 010 / offers.reminder_sent_at for
                                 that pattern) but was changed to a standing
                                 list; payment_reminder_sent_at is no longer
                                 written by this job.

    3. Upcoming deliveries    — orders with delivery_date within the next 5
                                 days (today through today+5 inclusive),
                                 regardless of dispatch_status. Deliberately
                                 recurring, not deduplicated: the same order
                                 is expected to appear on consecutive days as
                                 its delivery date approaches, which is useful
                                 planning information rather than spam.
                                 actual_delivery_date is never populated
                                 anywhere in this table (0 of 851 rows at the
                                 time this was built), so it cannot be used to
                                 detect "already delivered" — dispatch_status
                                 was considered as a filter but explicitly
                                 rejected: all 6 real matches on the day this
                                 was built were already dispatch_status=4, and
                                 the decision was that upcoming-delivery-date
                                 is calendar information worth surfacing either
                                 way.

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
DISPATCH_STATUS_LABEL = {1: "Not Dispatched", 3: "Partial Dispatch", 4: "Fully Dispatched"}


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
    SELECT id, invoice_number, company_name, sales_date, total_amount, payment_status
    FROM sales_orders
    WHERE payment_status = ANY(:statuses)
    ORDER BY sales_date ASC
    """
)

_UPCOMING_DELIVERIES_SQL = text(
    """
    SELECT id, invoice_number, company_name, delivery_date, total_amount, dispatch_status
    FROM sales_orders
    WHERE delivery_date BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '1 day' * :window_days
    ORDER BY delivery_date ASC
    """
)


def _fmt_money(amount: float) -> str:
    return f"₹{amount:,.2f}"


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
        ["Invoice", "Company", "Order Date", "Amount", "Payment Status"],
        [
            [
                escape(str(u["invoice_number"])),
                escape(str(u["company_name"] or "—")),
                _fmt_date(u["sales_date"]),
                _fmt_money(float(u["total_amount"])),
                escape(PAYMENT_STATUS_LABEL.get(u["payment_status"], "Unknown")),
            ]
            for u in unpaid
        ],
        "No unpaid sales orders.",
    )

    delivery_table = _table(
        ["Invoice", "Company", "Delivery Date", "Dispatch Status"],
        [
            [
                escape(str(d["invoice_number"])),
                escape(str(d["company_name"] or "—")),
                _fmt_date(d["delivery_date"]),
                escape(DISPATCH_STATUS_LABEL.get(d["dispatch_status"], "Unknown")),
            ]
            for d in deliveries
        ],
        f"No sales orders due for delivery in the next {DELIVERY_WINDOW_DAYS} days.",
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
            "Ops digest: %d open work order(s), %d unpaid sales order(s), "
            "%d delivery(ies) due within %d days",
            len(work_orders), len(unpaid), len(deliveries), DELIVERY_WINDOW_DAYS,
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
