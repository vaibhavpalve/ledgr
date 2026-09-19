"""SI-04's reminder emails - what the customer actually reads (ADR-071).

The wording is the product here: a friendly reminder that mentioned interest, or a
formal notice with no deadline, is the failure. Both languages, every step.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal

import pytest

from api.i18n.language import Language
from api.invoicing.delivery import (
    DeliverableInvoice,
    ReminderNotice,
    _reminder_body,
    _reminder_subject,
)

INVOICE = DeliverableInvoice(
    id=uuid.uuid4(),
    reference="2026-7",
    invoice_date=date(2025, 12, 1),
    due_date=date(2026, 1, 1),
    gross=Decimal("1210.00"),
    is_credit_note=False,
    supplier_name="Bakker Consultancy B.V.",
    supplier_email="info@bakker.example",
)

FRIENDLY = ReminderNotice(kind="friendly", outstanding=Decimal("1210.00"), days_overdue=10)
REMINDER = ReminderNotice(kind="reminder", outstanding=Decimal("1210.00"), days_overdue=25)
FORMAL = ReminderNotice(
    kind="formal_notice",
    outstanding=Decimal("1210.00"),
    days_overdue=40,
    interest=Decimal("13.55"),
    collection_cost=Decimal("181.50"),
    pay_by=date(2026, 2, 25),
)


@pytest.mark.parametrize("language", list(Language))
@pytest.mark.parametrize("notice", [FRIENDLY, REMINDER, FORMAL], ids=lambda n: n.kind)
def test_every_step_has_a_subject_and_body_in_every_language(
    language: Language, notice: ReminderNotice
) -> None:
    subject = _reminder_subject(INVOICE, language, notice)
    body = _reminder_body(INVOICE, language, notice)

    assert "2026-7" in subject and "Bakker Consultancy B.V." in subject
    assert "1.210,00" in body
    # No unfilled placeholder ever reaches a customer.
    assert "{" not in subject and "{" not in body
    assert body.rstrip().endswith("Bakker Consultancy B.V.")


@pytest.mark.parametrize("language", list(Language))
@pytest.mark.parametrize("notice", [FRIENDLY, REMINDER], ids=lambda n: n.kind)
def test_a_friendly_or_ordinary_reminder_claims_nothing_beyond_the_invoice(
    language: Language, notice: ReminderNotice
) -> None:
    """No interest, no costs, not even a mention of either - and no zero amount,
    which would put the idea in the customer's head."""
    body = _reminder_body(INVOICE, language, notice).lower()

    assert "rente" not in body and "interest" not in body
    assert "incassokosten" not in body and "collection cost" not in body
    # A standalone zero amount - not the "0,00" inside a real one like "1.210,00".
    assert not re.search(r"(?<![\d.,])0,00", body)


def test_the_subjects_escalate_in_tone() -> None:
    subjects = [_reminder_subject(INVOICE, Language.NL, n) for n in (FRIENDLY, REMINDER, FORMAL)]
    assert subjects[0].startswith("Herinnering")
    assert subjects[1].startswith("Betalingsherinnering")
    assert subjects[2].startswith("Aanmaning")


def test_the_english_formal_notice_says_so_plainly() -> None:
    assert _reminder_subject(INVOICE, Language.EN, FORMAL).startswith("Formal notice")


@pytest.mark.parametrize("language", list(Language))
def test_the_formal_notice_states_the_amounts_and_a_dated_deadline(language: Language) -> None:
    body = _reminder_body(INVOICE, language, FORMAL)

    assert "13,55" in body  # the statutory interest accrued
    assert "181,50" in body  # the collection cost that will be charged
    # A DATE, never 'promptly': the deadline is what makes the notice a notice.
    assert body.count("25-02-2026") == 2  # once in the demand, once in the cost warning


def test_the_collection_cost_is_a_conditional_statement_not_a_charge_today() -> None:
    """It announces what will be charged if payment is not received by the date."""
    dutch = _reminder_body(INVOICE, Language.NL, FORMAL)
    english = _reminder_body(INVOICE, Language.EN, FORMAL)

    assert "Betaalt u niet uiterlijk 25-02-2026" in dutch
    assert "If payment is not received by 25-02-2026" in english


def test_the_body_states_the_days_overdue_where_the_step_does() -> None:
    assert "25 dagen" in _reminder_body(INVOICE, Language.NL, REMINDER)
    assert "25 days" in _reminder_body(INVOICE, Language.EN, REMINDER)
