"""
Single entrypoint that runs both scheduled jobs.

This is what the Railway Cron Job's start command points at — the point of
this module is that both jobs always run, and a failure in one must not
prevent the other from running or from correctly reporting failure.
"""

from __future__ import annotations

from unittest.mock import patch

from app.jobs.daily_notifications import run


@patch("app.jobs.daily_notifications.ops_digest")
@patch("app.jobs.daily_notifications.offer_reminders")
def test_both_jobs_run_with_the_same_dry_run_flag(mock_offers, mock_digest):
    mock_offers.run.return_value = 0
    mock_digest.run.return_value = 0

    run(dry_run=True)

    mock_offers.run.assert_called_once_with(dry_run=True)
    mock_digest.run.assert_called_once_with(dry_run=True)


@patch("app.jobs.daily_notifications.ops_digest")
@patch("app.jobs.daily_notifications.offer_reminders")
def test_both_succeed_gives_exit_code_zero(mock_offers, mock_digest):
    mock_offers.run.return_value = 0
    mock_digest.run.return_value = 0
    assert run(dry_run=False) == 0


@patch("app.jobs.daily_notifications.ops_digest")
@patch("app.jobs.daily_notifications.offer_reminders")
def test_offer_reminders_failing_still_runs_ops_digest(mock_offers, mock_digest):
    """A crash in one job must not prevent the other from running at all."""
    mock_offers.run.return_value = 1
    mock_digest.run.return_value = 0

    exit_code = run(dry_run=False)

    mock_digest.run.assert_called_once()
    assert exit_code == 1


@patch("app.jobs.daily_notifications.ops_digest")
@patch("app.jobs.daily_notifications.offer_reminders")
def test_ops_digest_failing_does_not_hide_that_offer_reminders_already_ran(mock_offers, mock_digest):
    mock_offers.run.return_value = 0
    mock_digest.run.return_value = 1

    exit_code = run(dry_run=False)

    mock_offers.run.assert_called_once()
    assert exit_code == 1


@patch("app.jobs.daily_notifications.ops_digest")
@patch("app.jobs.daily_notifications.offer_reminders")
def test_both_failing_is_still_a_single_nonzero_exit(mock_offers, mock_digest):
    mock_offers.run.return_value = 1
    mock_digest.run.return_value = 1
    assert run(dry_run=False) == 1
