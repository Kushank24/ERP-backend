"""
15-day open-offer follow-up reminder.

Intended to run once a day as a standalone process (Railway Cron Job), not
inside the web server:

    python -m app.jobs.offer_reminders            # sends real emails
    python -m app.jobs.offer_reminders --dry-run   # logs what it would do, sends nothing

Rule, by explicit decision, not "15 or more days":

    An offer qualifies the day it turns EXACTLY 15 days old and is still
    status='sent' (i.e. still awaiting the customer's response).

That exact-match is deliberate. At the time this was built the database held
1,640 offers already open 15+ days across 1,019 companies (some over a year
old, one company with 70 open offers). A ">= 15 days" rule would have emailed
that entire backlog on the first run and keeps re-matching every open offer
every day forever unless separately throttled. "Exactly 15" makes each offer
eligible on exactly one calendar day, which is by itself enough to prevent
re-sending — the offers.reminder_sent_at column (migration 009) exists only
as a same-day double-fire guard and an audit trail, not as the primary
dedup mechanism.

Known trade-off of the exact-day rule: if this job does not run on the day an
offer turns 15 (crash, missed cron, DB outage), that offer is never reminded
— the next day it is 16 days old and no longer matches. Accepted as the cost
of never reaching into the backlog; do not "fix" it by widening the query to
`<=` without re-deciding that trade-off first.

One email per COMPANY per run, not per offer: if a company has more than one
offer landing on day-15 in the same run, they are combined into a single
email — one section per offer, one PDF attachment per offer — rather than
sending the recipient several separate emails within minutes of each other.

Each offer's PDF is generated with the exact same data and code path as a
manual download (`app.routers.offers._serialize` + the shared
PDFGenerationService instance), so the attachment always matches what the
customer would get by clicking "Download PDF" in the app.

Offers with no company on file, or a company with no email address, are
skipped and logged — never silently dropped.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..email_service import send_transactional_email
from ..logging_config import configure_logging
from ..routers.offers import _pdf_svc, _serialize

logger = logging.getLogger(__name__)

REMINDER_AGE_DAYS = 15

# Hosted on Google Drive rather than attached: the catalogue is 21.5 MB,
# which base64-inflates to ~29 MB as an email attachment — over Gmail's 25MB
# inbound limit and many corporate mail gateways' stricter caps. Several
# recipients are large industrial buyers whose IT policy would likely reject
# it outright, so a link is used instead.
#
# Requires the Drive file's sharing setting to be "Anyone with the link can
# view" — if it is restricted to specific accounts, recipients will hit a
# permission-denied page instead of the catalogue.
CATALOGUE_URL = "https://drive.google.com/file/d/1qwHrGsl_DhJABAflvZilXZCb0gPiUnfe/view?usp=sharing"

# Blind-copied on every reminder so there is a record of exactly what each
# customer received (subject, body, attached PDF) sitting in a normal inbox
# — this is the address the FROM header already uses, so replies also land
# here regardless of this setting.
INTERNAL_BCC = "esafe@esafe.co.in"


@dataclass
class OfferRow:
    id: int
    offer_number: str
    offer_date: str
    total_amount: float
    company_id: int
    company_name: str
    company_email: str | None
    contact_person: str | None


@dataclass
class CompanyBatch:
    company_id: int
    company_name: str
    company_email: str
    contact_person: str | None
    offers: list[OfferRow] = field(default_factory=list)


_CANDIDATES_SQL = text(
    """
    SELECT
        o.id, o.offer_number, o.offer_date::text AS offer_date, o.total_amount,
        o.company_id, c.name AS company_name, c.email AS company_email,
        COALESCE(o.kind_attn, c.contact_person) AS contact_person
    FROM offers o
    JOIN companies c ON c.id = o.company_id
    WHERE o.status = 'sent'
      AND o.offer_date = CURRENT_DATE - INTERVAL '1 day' * :age_days
      AND o.reminder_sent_at IS NULL
    ORDER BY o.company_id, o.id
    """
)

_MARK_SENT_SQL = text(
    "UPDATE offers SET reminder_sent_at = now() WHERE id = ANY(:ids)"
)


def _fmt_money(amount: float) -> str:
    return f"₹{amount:,.2f}"


def _fmt_date(iso_date: str) -> str:
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d %b %Y")


def _offer_section_html(offer_detail: dict) -> str:
    """
    One offer's block within the email body: when it was received, the items
    enquired, and its total. `offer_detail` is the same dict shape
    `_serialize()` returns and `generate_offer_pdf()` consumes, so this always
    matches the attached PDF and the in-app offer view.
    """
    rows = []
    for item in offer_detail.get("items", []):
        desc = escape(str(item.get("description") or ""))
        qty = item.get("quantity")
        unit = escape(str(item.get("unit") or "PC"))
        specs = item.get("specifications") or []
        spec_bits = "; ".join(
            f"{escape(str(s.get('spec_name') or ''))}: {escape(str(s.get('value') or ''))}"
            for s in specs
            if s.get("value")
        )
        spec_html = (
            f"<br><span style='color:#666;font-size:12px'>{spec_bits}</span>"
            if spec_bits
            else ""
        )
        rows.append(
            f"<tr>"
            f"<td style='padding:4px 12px 4px 0;vertical-align:top'>{desc}{spec_html}</td>"
            f"<td style='padding:4px 12px;vertical-align:top;white-space:nowrap'>{qty} {unit}</td>"
            f"</tr>"
        )
    items_table = (
        f"<table style='margin:8px 0 4px;font-size:14px;width:100%'>"
        f"<tr style='font-weight:600;color:#555'>"
        f"<td style='padding:4px 12px 4px 0'>Item</td>"
        f"<td style='padding:4px 12px'>Quantity</td>"
        f"</tr>{''.join(rows)}</table>"
        if rows
        else ""
    )

    offer_number = escape(str(offer_detail["offer_number"]))
    received_on = _fmt_date(str(offer_detail["offer_date"]))
    total = _fmt_money(float(offer_detail["total_amount"]))

    return f"""
      <div style="margin:16px 0;padding:12px 16px;border:1px solid #e2e2e2;border-radius:6px">
        <p style="margin:0 0 6px">
          <strong>Offer {offer_number}</strong> — received on {received_on},
          total {total}
        </p>
        {items_table}
        <p style="margin:6px 0 0;font-size:13px;color:#666">
          A copy of this offer is attached as a PDF for your reference.
        </p>
      </div>
    """.strip()


def _build_email(
    batch: CompanyBatch, offer_details: dict[int, dict]
) -> tuple[str, str]:
    """
    Returns (subject, html). `offer_details` maps offer id -> the full
    _serialize() dict for each offer in this batch (already fetched by the
    caller, once each, and reused for the PDF attachment).
    """
    greeting = escape(batch.contact_person or batch.company_name)
    plural = len(batch.offers) > 1

    if plural:
        subject = f"Following up on {len(batch.offers)} open offers from E-SAFE"
        lead = (
            "This is a gentle reminder regarding the following offers we "
            "shared with you, which are still awaiting your response:"
        )
    else:
        subject = f"Following up on Offer {batch.offers[0].offer_number} from E-SAFE"
        lead = (
            "This is a gentle reminder regarding the offer we shared with "
            "you, which is still awaiting your response:"
        )

    sections = "".join(
        _offer_section_html(offer_details[o.id]) for o in batch.offers
    )

    catalogue_html = (
        f'<p>You may also view our full product catalogue here: '
        f'<a href="{escape(CATALOGUE_URL)}">E-SAFE Product Catalogue (PDF)</a></p>'
    )

    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#222">
      <p>Dear {greeting},</p>
      <p>{lead}</p>
      {sections}
      <p>
        We remain fully open to any customisations, modifications, or
        adjustments to specifications, quantities, or pricing that may suit
        your requirements — please do let us know and we will be happy to
        revise the offer accordingly.
      </p>
      {catalogue_html}
      <p>Please feel free to reach out with any questions, or to let us know
        how you would like to proceed.</p>
      <p>
        Best regards,<br>
        <strong>E-SAFE Enterprises</strong><br>
        <span style="color:#666;font-size:13px">
          G-176, Boranada Industrial Park, Jodhpur-342012 (RAJ.)<br>
          Phone: +91 291 2944321 · Mobile: +91 94133 24321<br>
          Email: esafe@esafe.co.in · www.esafe.co.in
        </span>
      </p>
    </div>
    """.strip()

    return subject, html


def _fetch_candidates(db: Session) -> list[OfferRow]:
    rows = db.execute(_CANDIDATES_SQL, {"age_days": REMINDER_AGE_DAYS}).mappings().all()
    return [
        OfferRow(
            id=r["id"],
            offer_number=r["offer_number"],
            offer_date=r["offer_date"],
            total_amount=float(r["total_amount"]),
            company_id=r["company_id"],
            company_name=r["company_name"] or f"Company #{r['company_id']}",
            company_email=(r["company_email"] or "").strip() or None,
            contact_person=r["contact_person"],
        )
        for r in rows
    ]


def _group_by_company(offers: list[OfferRow]) -> list[CompanyBatch]:
    batches: dict[int, CompanyBatch] = {}
    for o in offers:
        if o.company_email is None:
            logger.warning(
                "Skipping offer %s (%s) — company %r (id=%s) has no email on file",
                o.id, o.offer_number, o.company_name, o.company_id,
            )
            continue
        batch = batches.setdefault(
            o.company_id,
            CompanyBatch(
                company_id=o.company_id,
                company_name=o.company_name,
                company_email=o.company_email,
                contact_person=o.contact_person,
            ),
        )
        batch.offers.append(o)
    return list(batches.values())


def _build_attachments(
    batch: CompanyBatch, offer_details: dict[int, dict]
) -> list[tuple[str, bytes, str]]:
    attachments = []
    for o in batch.offers:
        detail = offer_details[o.id]
        pdf_buf = _pdf_svc.generate_offer_pdf(detail, variant="normal")
        safe_num = o.offer_number.replace("/", "-")
        attachments.append((f"Offer-{safe_num}.pdf", pdf_buf.getvalue(), "application/pdf"))
    return attachments


def run(dry_run: bool) -> int:
    """Returns the process exit code."""
    with SessionLocal() as db:
        candidates = _fetch_candidates(db)
        logger.info(
            "Offer reminders: %d offer(s) turned exactly %d days old and unreminded",
            len(candidates), REMINDER_AGE_DAYS,
        )

        batches = _group_by_company(candidates)
        logger.info(
            "Grouped into %d compan%s to email",
            len(batches), "y" if len(batches) == 1 else "ies",
        )

        sent_offer_ids: list[int] = []
        failures = 0

        for batch in batches:
            # Fetch full detail (items, specs) once per offer — the same
            # shape used by the manual "Download PDF" endpoint — and reuse it
            # for both the email body and the attached PDF.
            offer_details = {o.id: _serialize(db, o.id) for o in batch.offers}

            subject, html = _build_email(batch, offer_details)
            offer_numbers = ", ".join(o.offer_number for o in batch.offers)

            if dry_run:
                logger.info(
                    "[DRY RUN] Would email %s <%s> — %s (with %d PDF attachment(s))",
                    batch.company_name, batch.company_email, offer_numbers, len(batch.offers),
                )
                continue

            try:
                attachments = _build_attachments(batch, offer_details)
                send_transactional_email(
                    batch.company_email, subject, html, attachments, bcc=INTERNAL_BCC
                )
            except Exception as exc:
                failures += 1
                logger.error(
                    "Reminder failed for %s <%s> (offers: %s): %s",
                    batch.company_name, batch.company_email, offer_numbers, exc,
                )
                continue

            logger.info(
                "Reminder sent to %s <%s> — %s", batch.company_name, batch.company_email, offer_numbers
            )
            sent_offer_ids.extend(o.id for o in batch.offers)

        if sent_offer_ids:
            db.execute(_MARK_SENT_SQL, {"ids": sent_offer_ids})
            db.commit()
            logger.info("Marked %d offer(s) as reminded", len(sent_offer_ids))
        elif not dry_run:
            db.commit()

        skipped_no_email = len(candidates) - sum(len(b.offers) for b in batches)
        logger.info(
            "Done. companies_emailed=%d offers_marked=%d failures=%d skipped_no_email=%d dry_run=%s",
            len(batches) - failures, len(sent_offer_ids), failures, skipped_no_email, dry_run,
        )

        return 1 if failures else 0


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
