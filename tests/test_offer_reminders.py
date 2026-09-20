"""
15-day open-offer reminder job.

The exact-day matching rule and the reminder_sent_at idempotency guard exist
because the database held 1,640 offers already open 15+ days across 1,019
companies when this was built (one company with 70). A ">= 15 days" rule
would have emailed that entire backlog on the first run. These tests pin the
behaviour that prevents that, the per-company grouping, the catalogue link,
and the PDF-attachment path added when the reminder was extended to include
the enquired items and a copy of the offer.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from app.jobs.offer_reminders import (
    CATALOGUE_URL,
    REMINDER_AGE_DAYS,
    _MARK_SENT_SQL,
    CompanyBatch,
    OfferRow,
    _build_attachments,
    _build_email,
    _fetch_candidates,
    _fmt_date,
    _fmt_money,
    _group_by_company,
    _offer_section_html,
    run,
)


def _offer(**kw) -> OfferRow:
    defaults = dict(
        id=1, offer_number="ES/26-27/A-1/0001", offer_date="2026-09-05",
        total_amount=16402.0, company_id=1, company_name="Acme Corp",
        company_email="buyer@acme.com", contact_person="Mr Buyer",
    )
    defaults.update(kw)
    return OfferRow(**defaults)


def _detail(offer: OfferRow, items: list[dict] | None = None) -> dict:
    """
    A _serialize()-shaped dict — what the real function returns and what
    generate_offer_pdf() and _offer_section_html() both consume. Kept minimal
    but with the exact keys those two read.
    """
    return {
        "id": offer.id,
        "offer_number": offer.offer_number,
        "offer_date": offer.offer_date,
        "total_amount": offer.total_amount,
        "items": items if items is not None else [
            {"description": "Aluminium Extension Ladder", "quantity": 2, "unit": "PC", "specifications": []}
        ],
    }


# ---------------------------------------------------------------------------
# The exact-day rule
# ---------------------------------------------------------------------------


def test_reminder_age_is_exactly_15_not_a_minimum():
    """
    Pinned so a future edit cannot silently widen this to `>=`, which is
    exactly the change that would re-open the 1,640-offer backlog.
    """
    assert REMINDER_AGE_DAYS == 15


# ---------------------------------------------------------------------------
# Grouping per company
# ---------------------------------------------------------------------------


def test_single_offer_per_company_is_its_own_batch():
    batches = _group_by_company([_offer()])
    assert len(batches) == 1
    assert len(batches[0].offers) == 1


def test_two_offers_same_company_become_one_batch():
    offers = [
        _offer(id=1, offer_number="ES/1", company_id=7),
        _offer(id=2, offer_number="ES/2", company_id=7),
    ]
    batches = _group_by_company(offers)
    assert len(batches) == 1
    assert {o.id for o in batches[0].offers} == {1, 2}


def test_offers_from_different_companies_stay_separate():
    offers = [_offer(id=1, company_id=1), _offer(id=2, company_id=2)]
    assert len(_group_by_company(offers)) == 2


def test_batch_carries_the_companys_email_and_contact():
    batches = _group_by_company([_offer(company_email="x@y.com", contact_person="Priya")])
    assert batches[0].company_email == "x@y.com"
    assert batches[0].contact_person == "Priya"


# ---------------------------------------------------------------------------
# Skip, don't drop, companies with no email
# ---------------------------------------------------------------------------


def test_offer_with_no_company_email_is_skipped_not_grouped():
    assert _group_by_company([_offer(company_email=None)]) == []


def test_one_missing_email_does_not_block_other_companies_in_the_same_run():
    offers = [
        _offer(id=1, company_id=1, company_email=None),
        _offer(id=2, company_id=2, company_email="ok@company.com"),
    ]
    batches = _group_by_company(offers)
    assert len(batches) == 1
    assert batches[0].company_id == 2


def test_blank_email_string_is_treated_as_missing():
    """companies.email can be '' rather than NULL — both must skip."""
    fake_row = {
        "id": 1, "offer_number": "ES/1", "offer_date": "2026-09-05",
        "total_amount": 100.0, "company_id": 1, "company_name": "Acme",
        "company_email": "   ", "contact_person": None,
    }
    db = MagicMock()
    db.execute.return_value.mappings.return_value.all.return_value = [fake_row]
    rows = _fetch_candidates(db)
    assert rows[0].company_email is None


# ---------------------------------------------------------------------------
# Per-offer section: items, specs, escaping
# ---------------------------------------------------------------------------


def test_offer_section_states_number_date_and_total():
    offer = _offer()
    html = _offer_section_html(_detail(offer))
    assert "ES/26-27/A-1/0001" in html
    assert "05 Sep 2026" in html
    assert "16,402.00" in html


def test_offer_section_lists_each_enquired_item():
    offer = _offer()
    detail = _detail(offer, items=[
        {"description": "Extension Ladder", "quantity": 2, "unit": "PC", "specifications": []},
        {"description": "Platform Ladder", "quantity": 1, "unit": "PC", "specifications": []},
    ])
    html = _offer_section_html(detail)
    assert "Extension Ladder" in html
    assert "Platform Ladder" in html
    assert "2 PC" in html
    assert "1 PC" in html


def test_offer_section_includes_specifications_when_present():
    offer = _offer()
    detail = _detail(offer, items=[
        {
            "description": "Extension Ladder", "quantity": 1, "unit": "PC",
            "specifications": [{"spec_name": "Height", "value": "3.5m"}],
        },
    ])
    html = _offer_section_html(detail)
    assert "Height: 3.5m" in html


def test_offer_section_omits_specs_with_blank_value():
    offer = _offer()
    detail = _detail(offer, items=[
        {
            "description": "Ladder", "quantity": 1, "unit": "PC",
            "specifications": [{"spec_name": "Colour", "value": ""}],
        },
    ])
    html = _offer_section_html(detail)
    assert "Colour" not in html


def test_offer_section_with_no_items_omits_the_table_but_still_shows_the_header():
    offer = _offer()
    html = _offer_section_html(_detail(offer, items=[]))
    assert "ES/26-27/A-1/0001" in html
    assert "<table" not in html


def test_offer_section_mentions_the_attached_pdf():
    html = _offer_section_html(_detail(_offer()))
    assert "attached as a PDF" in html


def test_item_description_is_html_escaped():
    offer = _offer()
    detail = _detail(offer, items=[
        {"description": "Ladder & Co <Special>", "quantity": 1, "unit": "PC", "specifications": []}
    ])
    html = _offer_section_html(detail)
    assert "Ladder &amp; Co &lt;Special&gt;" in html
    assert "<Special>" not in html


def test_money_formatting():
    assert _fmt_money(16402.0) == "₹16,402.00"
    assert _fmt_money(0) == "₹0.00"


def test_date_formatting():
    assert _fmt_date("2026-09-05") == "05 Sep 2026"


# ---------------------------------------------------------------------------
# Full email: subject, greeting, catalogue link
# ---------------------------------------------------------------------------


def test_single_offer_subject_names_the_offer():
    offer = _offer()
    batch = CompanyBatch(1, "Acme", "a@b.com", "Mr X", [offer])
    subject, _ = _build_email(batch, {offer.id: _detail(offer)})
    assert subject == "Following up on Offer ES/26-27/A-1/0001"


def test_multi_offer_subject_gives_a_count():
    o1, o2 = _offer(id=1, offer_number="ES/1"), _offer(id=2, offer_number="ES/2")
    batch = CompanyBatch(1, "Acme", "a@b.com", "Mr X", [o1, o2])
    subject, _ = _build_email(batch, {1: _detail(o1), 2: _detail(o2)})
    assert subject == "Following up on 2 open offers"


def test_multi_offer_email_renders_a_section_per_offer():
    o1, o2 = _offer(id=1, offer_number="ES/1"), _offer(id=2, offer_number="ES/2")
    batch = CompanyBatch(1, "Acme", "a@b.com", "Mr X", [o1, o2])
    _, html = _build_email(batch, {1: _detail(o1), 2: _detail(o2)})
    assert "ES/1" in html
    assert "ES/2" in html


def test_greeting_prefers_contact_person_over_company_name():
    offer = _offer()
    batch = CompanyBatch(1, "Acme Corp", "a@b.com", "Priya Sharma", [offer])
    _, html = _build_email(batch, {offer.id: _detail(offer)})
    assert "Dear Priya Sharma" in html


def test_greeting_falls_back_to_company_name_when_no_contact():
    offer = _offer()
    batch = CompanyBatch(1, "Acme Corp", "a@b.com", None, [offer])
    _, html = _build_email(batch, {offer.id: _detail(offer)})
    assert "Dear Acme Corp" in html


def test_company_name_is_html_escaped():
    offer = _offer()
    batch = CompanyBatch(1, "A & B Traders", "a@b.com", None, [offer])
    _, html = _build_email(batch, {offer.id: _detail(offer)})
    assert "A &amp; B Traders" in html


def test_email_mentions_openness_to_customisation():
    """Explicit requirement: the body must invite customisation requests."""
    offer = _offer()
    batch = CompanyBatch(1, "Acme", "a@b.com", None, [offer])
    _, html = _build_email(batch, {offer.id: _detail(offer)})
    assert "customisation" in html.lower()


def test_email_links_to_the_catalogue():
    offer = _offer()
    batch = CompanyBatch(1, "Acme", "a@b.com", None, [offer])
    _, html = _build_email(batch, {offer.id: _detail(offer)})
    assert CATALOGUE_URL in html


def test_catalogue_url_is_a_drive_link_with_view_sharing():
    """
    Hosted on Drive rather than attached — the file is 21.5 MB and would
    base64-inflate past Gmail's 25MB inbound limit. Pinned so this doesn't
    silently regress back to an attachment.
    """
    assert CATALOGUE_URL.startswith("https://drive.google.com/")
    assert "usp=sharing" in CATALOGUE_URL


def test_email_signs_off_as_esafe_enterprises():
    offer = _offer()
    batch = CompanyBatch(1, "Acme", "a@b.com", None, [offer])
    _, html = _build_email(batch, {offer.id: _detail(offer)})
    assert "E-SAFE Enterprises" in html


# ---------------------------------------------------------------------------
# PDF attachments — one per offer, matching the offer_number
# ---------------------------------------------------------------------------


@patch("app.jobs.offer_reminders._pdf_svc")
def test_one_pdf_attachment_per_offer(mock_pdf_svc):
    mock_pdf_svc.generate_offer_pdf.return_value = io.BytesIO(b"%PDF-fake")
    o1, o2 = _offer(id=1, offer_number="ES/1"), _offer(id=2, offer_number="ES/2")
    batch = CompanyBatch(1, "Acme", "a@b.com", None, [o1, o2])
    attachments = _build_attachments(batch, {1: _detail(o1), 2: _detail(o2)})
    assert len(attachments) == 2
    assert mock_pdf_svc.generate_offer_pdf.call_count == 2


@patch("app.jobs.offer_reminders._pdf_svc")
def test_pdf_filename_sanitises_slashes_in_the_offer_number(mock_pdf_svc):
    mock_pdf_svc.generate_offer_pdf.return_value = io.BytesIO(b"%PDF-fake")
    offer = _offer(offer_number="ES/26-27/A-1/0924")
    batch = CompanyBatch(1, "Acme", "a@b.com", None, [offer])
    filename, _, mime = _build_attachments(batch, {offer.id: _detail(offer)})[0]
    assert filename == "Offer-ES-26-27-A-1-0924.pdf"
    assert mime == "application/pdf"


@patch("app.jobs.offer_reminders._pdf_svc")
def test_pdf_is_generated_with_normal_variant_matching_manual_download(mock_pdf_svc):
    mock_pdf_svc.generate_offer_pdf.return_value = io.BytesIO(b"%PDF-fake")
    offer = _offer()
    detail = _detail(offer)
    batch = CompanyBatch(1, "Acme", "a@b.com", None, [offer])
    _build_attachments(batch, {offer.id: detail})
    mock_pdf_svc.generate_offer_pdf.assert_called_once_with(detail, variant="normal")


# ---------------------------------------------------------------------------
# run(): orchestration — dry-run, failures, idempotency
# ---------------------------------------------------------------------------
# _serialize and _pdf_svc are patched directly rather than reconstructing the
# several distinct SQL queries _serialize() issues internally — this isolates
# run()'s own orchestration logic (skip/group/send/mark) from that function's
# implementation, which has its own tests via the offers router.


def _candidate_row(id_, company_id=1, email="a@b.com", offer_number=None):
    return {
        "id": id_, "offer_number": offer_number or f"ES/{id_}", "offer_date": "2026-09-05",
        "total_amount": 100.0, "company_id": company_id, "company_name": f"Company{company_id}",
        "company_email": email, "contact_person": None,
    }


@patch("app.jobs.offer_reminders._pdf_svc")
@patch("app.jobs.offer_reminders._serialize")
@patch("app.jobs.offer_reminders.SessionLocal")
@patch("app.jobs.offer_reminders.send_transactional_email")
def test_dry_run_never_sends_or_generates_a_pdf(mock_send, mock_session, mock_serialize, mock_pdf_svc):
    db = MagicMock()
    mock_session.return_value.__enter__.return_value = db
    db.execute.return_value.mappings.return_value.all.return_value = [_candidate_row(1)]
    mock_serialize.return_value = {"id": 1, "offer_number": "ES/1", "offer_date": "2026-09-05",
                                    "total_amount": 100.0, "items": []}

    exit_code = run(dry_run=True)

    mock_send.assert_not_called()
    mock_pdf_svc.generate_offer_pdf.assert_not_called()
    assert exit_code == 0


@patch("app.jobs.offer_reminders._pdf_svc")
@patch("app.jobs.offer_reminders._serialize")
@patch("app.jobs.offer_reminders.SessionLocal")
@patch("app.jobs.offer_reminders.send_transactional_email")
def test_dry_run_does_not_update_reminder_sent_at(mock_send, mock_session, mock_serialize, mock_pdf_svc):
    db = MagicMock()
    mock_session.return_value.__enter__.return_value = db
    db.execute.return_value.mappings.return_value.all.return_value = [_candidate_row(1)]
    mock_serialize.return_value = {"id": 1, "offer_number": "ES/1", "offer_date": "2026-09-05",
                                    "total_amount": 100.0, "items": []}

    run(dry_run=True)

    update_calls = [c for c in db.execute.call_args_list if c.args and c.args[0] is _MARK_SENT_SQL]
    assert update_calls == []


@patch("app.jobs.offer_reminders._pdf_svc")
@patch("app.jobs.offer_reminders._serialize")
@patch("app.jobs.offer_reminders.SessionLocal")
@patch("app.jobs.offer_reminders.send_transactional_email")
def test_failed_send_is_not_marked_reminded(mock_send, mock_session, mock_serialize, mock_pdf_svc):
    db = MagicMock()
    mock_session.return_value.__enter__.return_value = db
    db.execute.return_value.mappings.return_value.all.return_value = [_candidate_row(1)]
    mock_serialize.return_value = {"id": 1, "offer_number": "ES/1", "offer_date": "2026-09-05",
                                    "total_amount": 100.0, "items": []}
    mock_pdf_svc.generate_offer_pdf.return_value = io.BytesIO(b"%PDF-fake")
    mock_send.side_effect = RuntimeError("Resend rejected message")

    exit_code = run(dry_run=False)

    assert exit_code == 1
    update_calls = [c for c in db.execute.call_args_list if c.args and c.args[0] is _MARK_SENT_SQL]
    assert update_calls == []


@patch("app.jobs.offer_reminders._pdf_svc")
@patch("app.jobs.offer_reminders._serialize")
@patch("app.jobs.offer_reminders.SessionLocal")
@patch("app.jobs.offer_reminders.send_transactional_email")
def test_successful_send_marks_only_that_offer(mock_send, mock_session, mock_serialize, mock_pdf_svc):
    db = MagicMock()
    mock_session.return_value.__enter__.return_value = db
    db.execute.return_value.mappings.return_value.all.return_value = [_candidate_row(42)]
    mock_serialize.return_value = {"id": 42, "offer_number": "ES/42", "offer_date": "2026-09-05",
                                    "total_amount": 100.0, "items": []}
    mock_pdf_svc.generate_offer_pdf.return_value = io.BytesIO(b"%PDF-fake")

    exit_code = run(dry_run=False)

    assert exit_code == 0
    mock_send.assert_called_once()
    update_calls = [c for c in db.execute.call_args_list if c.args and c.args[0] is _MARK_SENT_SQL]
    assert len(update_calls) == 1
    assert update_calls[0].args[1] == {"ids": [42]}


@patch("app.jobs.offer_reminders._pdf_svc")
@patch("app.jobs.offer_reminders._serialize")
@patch("app.jobs.offer_reminders.SessionLocal")
@patch("app.jobs.offer_reminders.send_transactional_email")
def test_one_company_failing_does_not_block_marking_another_that_succeeded(
    mock_send, mock_session, mock_serialize, mock_pdf_svc
):
    db = MagicMock()
    mock_session.return_value.__enter__.return_value = db
    db.execute.return_value.mappings.return_value.all.return_value = [
        _candidate_row(1, company_id=1, email="fail@co.com"),
        _candidate_row(2, company_id=2, email="ok@co.com"),
    ]
    mock_serialize.side_effect = lambda db_, oid: {
        "id": oid, "offer_number": f"ES/{oid}", "offer_date": "2026-09-05",
        "total_amount": 100.0, "items": [],
    }
    mock_pdf_svc.generate_offer_pdf.return_value = io.BytesIO(b"%PDF-fake")

    def side_effect(to_email, *_a, **_kw):
        if to_email == "fail@co.com":
            raise RuntimeError("boom")

    mock_send.side_effect = side_effect

    exit_code = run(dry_run=False)

    assert exit_code == 1
    update_calls = [c for c in db.execute.call_args_list if c.args and c.args[0] is _MARK_SENT_SQL]
    assert len(update_calls) == 1
    assert update_calls[0].args[1] == {"ids": [2]}


@patch("app.jobs.offer_reminders._pdf_svc")
@patch("app.jobs.offer_reminders._serialize")
@patch("app.jobs.offer_reminders.SessionLocal")
@patch("app.jobs.offer_reminders.send_transactional_email")
def test_send_receives_a_pdf_attachment(mock_send, mock_session, mock_serialize, mock_pdf_svc):
    """Guards against a future refactor silently dropping the attachment."""
    db = MagicMock()
    mock_session.return_value.__enter__.return_value = db
    db.execute.return_value.mappings.return_value.all.return_value = [_candidate_row(1)]
    mock_serialize.return_value = {"id": 1, "offer_number": "ES/1", "offer_date": "2026-09-05",
                                    "total_amount": 100.0, "items": []}
    mock_pdf_svc.generate_offer_pdf.return_value = io.BytesIO(b"%PDF-fake")

    run(dry_run=False)

    _, args, kwargs = mock_send.mock_calls[0]
    attachments = args[3] if len(args) > 3 else kwargs.get("attachments")
    assert attachments is not None
    assert len(attachments) == 1
    assert attachments[0][0] == "Offer-ES-1.pdf"
