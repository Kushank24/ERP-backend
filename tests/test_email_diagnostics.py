"""
Resend failure classification.

Context: campaigns were failing in production with nothing usable to go on.
The real reason was being written to ``email_campaign_failures.error`` as
``"Resend 403: error code: 1010"`` and nothing surfaced it — and because the
app configured no logging at all, every ``logger.info`` line was dropped by
``logging.lastResort`` too.

``error code: 1010`` is a *Cloudflare* code, not a Resend one: the request was
blocked before it reached Resend. Reading it as a Resend 403 sends you looking
at API keys and sender domains, which are fine. These tests pin that
distinction, because getting it wrong costs hours.
"""

from __future__ import annotations

import pytest

from app.email_service import _describe_resend_failure, _header


# ---------------------------------------------------------------------------
# Case-insensitive header lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["cf-ray", "CF-RAY", "Cf-Ray", "CF-Ray"])
def test_header_lookup_is_case_insensitive(key):
    """
    Cloudflare sends CF-RAY. A lowercase-only .get() dropped the Ray ID, which
    is the first identifier Cloudflare or Resend support asks for.
    """
    assert _header({key: "abc123-BOM"}, "cf-ray") == "abc123-BOM"


def test_header_lookup_handles_missing_and_empty():
    assert _header({}, "cf-ray") is None
    assert _header(None, "cf-ray") is None
    assert _header({"server": "cloudflare"}, "cf-ray") is None


# ---------------------------------------------------------------------------
# Cloudflare blocks — the actual production failure
# ---------------------------------------------------------------------------


def test_cloudflare_1010_is_not_blamed_on_the_api_key_or_sender():
    """The exact failure recorded for campaigns 11 and 12."""
    detail = _describe_resend_failure(
        403, "error code: 1010", {"CF-RAY": "a3da-BOM", "Server": "cloudflare"}
    )
    assert "cloudflare=1010" in detail
    assert "never reached Resend" in detail
    # Must NOT send the reader after an API key or a sender domain.
    assert "API key" not in detail
    assert "sender domain" not in detail


def test_cloudflare_block_surfaces_the_ray_id():
    detail = _describe_resend_failure(403, "error code: 1010", {"CF-RAY": "a3da0a65-BOM"})
    assert "cf-ray=a3da0a65-BOM" in detail


def test_cloudflare_1010_suggests_the_user_agent():
    detail = _describe_resend_failure(403, "error code: 1010", {})
    assert "User-Agent" in detail or "RESEND_USER_AGENT" in detail


def test_cloudflare_1020_is_described_as_a_firewall_rule():
    detail = _describe_resend_failure(403, "error code: 1020", {})
    assert "cloudflare=1020" in detail
    assert "firewall" in detail.lower()


def test_unknown_cloudflare_code_still_says_cloudflare():
    detail = _describe_resend_failure(403, "error code: 9999", {})
    assert "cloudflare=9999" in detail
    assert "before it reached Resend" in detail


# ---------------------------------------------------------------------------
# Genuine Resend API errors — must be diagnosed differently
# ---------------------------------------------------------------------------


def test_plain_403_without_a_cloudflare_code_points_at_the_sender_domain():
    detail = _describe_resend_failure(
        403, '{"statusCode":403,"message":"Domain not verified"}', {}
    )
    assert "sender domain" in detail
    assert "cloudflare" not in detail.lower()


def test_401_points_at_the_api_key_and_the_hosting_environment():
    detail = _describe_resend_failure(401, '{"message":"Invalid API key"}', {})
    assert "API key" in detail
    # The key being present locally but absent on the host is the usual cause.
    assert "hosting environment" in detail


def test_422_points_at_the_payload():
    detail = _describe_resend_failure(
        422, '{"statusCode":422,"name":"missing_required_field"}', {}
    )
    assert "payload" in detail


def test_429_points_at_the_send_rate():
    detail = _describe_resend_failure(429, "rate limited", {})
    assert "rate limit" in detail.lower()
    assert "SMTP_DELAY_SECONDS" in detail


def test_response_body_is_included_but_bounded():
    detail = _describe_resend_failure(500, "x" * 2000, {})
    assert "xxx" in detail
    assert len(detail) < 700


def test_newlines_in_the_body_are_flattened():
    """The message is written to a log line and a DB column."""
    detail = _describe_resend_failure(500, "line one\nline two", {})
    assert "\n" not in detail


# ---------------------------------------------------------------------------
# Configuration report
# ---------------------------------------------------------------------------


def test_diagnostics_never_returns_the_api_key(monkeypatch):
    from app import email_service
    from app.config import settings

    monkeypatch.setattr(settings, "resend_api_key", "re_SUPERSECRETVALUE123456", raising=False)
    report = email_service.resend_diagnostics(probe=False)

    blob = repr(report)
    assert "SUPERSECRETVALUE" not in blob
    assert report["resend_api_key_present"] is True
    assert report["resend_api_key_length"] == len("re_SUPERSECRETVALUE123456")
    assert report["resend_api_key_prefix"].startswith("re_")


def test_diagnostics_flags_a_public_mailbox_sender(monkeypatch):
    from app import email_service
    from app.config import settings

    monkeypatch.setattr(settings, "resend_from_email", "someone@gmail.com", raising=False)
    report = email_service.resend_diagnostics(probe=False)
    assert any("gmail.com" in w for w in report["warnings"])


def test_diagnostics_flags_a_missing_key_when_resend_is_forced(monkeypatch):
    from app import email_service
    from app.config import settings

    monkeypatch.setattr(settings, "email_provider", "resend", raising=False)
    monkeypatch.setattr(settings, "resend_api_key", "", raising=False)
    report = email_service.resend_diagnostics(probe=False)
    assert report["provider_that_would_be_used"] == "resend"
    assert any("RESEND_API_KEY is empty" in w for w in report["warnings"])


def test_diagnostics_reports_which_provider_auto_would_choose(monkeypatch):
    from app import email_service
    from app.config import settings

    monkeypatch.setattr(settings, "email_provider", "auto", raising=False)
    monkeypatch.setattr(settings, "resend_api_key", "", raising=False)
    assert email_service.resend_diagnostics(probe=False)["provider_that_would_be_used"] == "smtp"

    monkeypatch.setattr(settings, "resend_api_key", "re_abcdefghijklmnopqrst", raising=False)
    assert email_service.resend_diagnostics(probe=False)["provider_that_would_be_used"] == "resend"


def test_diagnostics_records_where_the_from_address_came_from(monkeypatch):
    from app import email_service
    from app.config import settings

    monkeypatch.setattr(settings, "resend_from_email", "", raising=False)
    monkeypatch.setattr(settings, "smtp_user", "esafe@esafe.co.in", raising=False)
    report = email_service.resend_diagnostics(probe=False)
    assert report["from_address"] == "esafe@esafe.co.in"
    assert "fallback" in report["from_address_source"]
    assert report["from_domain"] == "esafe.co.in"


def test_probe_can_be_skipped():
    """probe=false must make no network call."""
    from app import email_service

    assert email_service.resend_diagnostics(probe=False)["probe"] is None


# ---------------------------------------------------------------------------
# Logging is configured at all — the reason none of this was visible
# ---------------------------------------------------------------------------


def test_logging_configuration_attaches_a_root_handler():
    import logging

    from app.logging_config import configure_logging

    configure_logging()
    assert logging.getLogger().handlers, "no root handler — INFO lines would be dropped"


def test_configure_logging_is_idempotent():
    import logging

    from app.logging_config import configure_logging

    configure_logging()
    before = len(logging.getLogger().handlers)
    configure_logging()
    assert len(logging.getLogger().handlers) == before


def test_app_import_configures_logging():
    """main.py must configure logging before anything serves a request."""
    import logging

    import app.main  # noqa: F401

    assert logging.getLogger().handlers
