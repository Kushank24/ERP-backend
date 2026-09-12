"""
Sliding-window throttle on /auth/login.

/auth/login is one of only two endpoints reachable without a bearer token, so
it is the brute-force target. These tests exercise the window bookkeeping
directly — they do not need a database or an HTTP client.
"""

from __future__ import annotations

import time

import pytest

from app.routers import auth


@pytest.fixture(autouse=True)
def clean_state():
    """Each test starts with an empty failure table."""
    auth._failures.clear()
    yield
    auth._failures.clear()


def test_allows_attempts_below_the_threshold():
    keys = ["ip:1.2.3.4"]
    for _ in range(auth._MAX_FAILURES - 1):
        auth._check_not_throttled(keys)
        auth._record_failure(keys)
    # Still under the limit — must not raise.
    auth._check_not_throttled(keys)


def test_blocks_once_the_threshold_is_reached():
    keys = ["ip:1.2.3.4"]
    for _ in range(auth._MAX_FAILURES):
        auth._record_failure(keys)
    with pytest.raises(Exception) as exc:
        auth._check_not_throttled(keys)
    assert getattr(exc.value, "status_code", None) == 429


def test_block_response_carries_retry_after():
    keys = ["ip:9.9.9.9"]
    for _ in range(auth._MAX_FAILURES):
        auth._record_failure(keys)
    with pytest.raises(Exception) as exc:
        auth._check_not_throttled(keys)
    assert exc.value.headers["Retry-After"] == str(auth._WINDOW_SECONDS)


def test_successful_login_clears_the_counter():
    keys = ["ip:1.2.3.4", "user:alice"]
    for _ in range(auth._MAX_FAILURES):
        auth._record_failure(keys)
    auth._clear(keys)
    auth._check_not_throttled(keys)  # must not raise


def test_throttling_is_scoped_per_key():
    """One attacker IP must not lock out an unrelated one."""
    for _ in range(auth._MAX_FAILURES):
        auth._record_failure(["ip:10.0.0.1"])
    with pytest.raises(Exception):
        auth._check_not_throttled(["ip:10.0.0.1"])
    auth._check_not_throttled(["ip:10.0.0.2"])  # unaffected


def test_username_key_blocks_password_spraying_across_ips():
    """
    Rotating source IPs while hammering one account still trips the per-username
    counter, because login records a failure under both keys.
    """
    for i in range(auth._MAX_FAILURES):
        auth._record_failure([f"ip:172.16.0.{i}", "user:admin"])
    with pytest.raises(Exception):
        auth._check_not_throttled(["ip:172.16.99.99", "user:admin"])


def test_entries_outside_the_window_are_forgotten():
    keys = ["ip:1.2.3.4"]
    stale = time.time() - auth._WINDOW_SECONDS - 1
    hits = auth._prune("ip:1.2.3.4", time.time())
    for _ in range(auth._MAX_FAILURES):
        hits.append(stale)
    # All recorded failures predate the window, so the caller is clear again.
    auth._check_not_throttled(keys)
    assert len(auth._failures["ip:1.2.3.4"]) == 0


def test_failure_table_stays_bounded():
    """A distributed attack must not grow the table without limit."""
    for i in range(auth._MAX_TRACKED_KEYS + 500):
        auth._record_failure([f"ip:203.0.113.{i}"])
    assert len(auth._failures) <= auth._MAX_TRACKED_KEYS
