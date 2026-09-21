"""
Single entrypoint for everything the daily scheduled cron should run.

This is what the Railway Cron Job should actually point its start command
at — not the individual job modules directly — so that adding a fourth check
later means adding one line here, not reconfiguring the cron schedule again.

    python -m app.jobs.daily_notifications
    python -m app.jobs.daily_notifications --dry-run

Runs, in order:
  1. app.jobs.offer_reminders — customer-facing, one email per company with
     an open offer that turned exactly 15 days old today. BCC'd to
     esafe@esafe.co.in.
  2. app.jobs.ops_digest — one internal email to accounts@esafe.co.in
     covering open work orders, sales orders that turned exactly 10 days old
     unpaid today, and sales orders due for delivery in the next 5 days.

Each job's own module remains independently runnable (see their docstrings)
for isolated testing — this module only sequences them and combines the exit
code. A failure in one job does not skip the other: both always run, and the
process exit code is non-zero if either reported a failure.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..logging_config import configure_logging
from . import offer_reminders, ops_digest

logger = logging.getLogger(__name__)


def run(dry_run: bool) -> int:
    logger.info("=== Daily notifications: starting (dry_run=%s) ===", dry_run)

    logger.info("--- 1/2: offer reminders ---")
    offer_exit = offer_reminders.run(dry_run=dry_run)

    logger.info("--- 2/2: ops digest ---")
    digest_exit = ops_digest.run(dry_run=dry_run)

    overall = 1 if (offer_exit or digest_exit) else 0
    logger.info(
        "=== Daily notifications: done. offer_reminders_exit=%d ops_digest_exit=%d overall=%d ===",
        offer_exit, digest_exit, overall,
    )
    return overall


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what each job would do without sending or marking anything.",
    )
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
