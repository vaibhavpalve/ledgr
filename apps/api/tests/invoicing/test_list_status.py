"""What the sales invoice list says about each row's money-state.

`payment_status` is pure, so the whole decision table is asserted here without a
database; `InvoicingService.list_rows` is checked against a minimal fake for the
parts that combine facts (payment date only once something is paid).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from api.invoicing.list_status import InvoiceListFacts, PaymentStatus, payment_status
from api.invoicing.model import InvoiceStatus
from tests.invoicing.test_list_invoices import FakeListRepository, _invoice, _service

TODAY = date(2026, 9, 24)
OWED = Decimal("100.00")


@pytest.mark.parametrize(
    ("facts", "due", "expected"),
    [
        (InvoiceListFacts(outstanding=OWED), date(2026, 10, 1), PaymentStatus.OPEN),
        (InvoiceListFacts(outstanding=OWED), date(2026, 9, 23), PaymentStatus.OVERDUE),
        # Due today is not yet late: the date has to have passed.
        (InvoiceListFacts(outstanding=OWED), TODAY, PaymentStatus.OPEN),
        (
            InvoiceListFacts(outstanding=OWED, paid=Decimal("40.00")),
            date(2026, 10, 1),
            PaymentStatus.PARTIALLY_PAID,
        ),
        # Part-paid and past due is still overdue: the words a person acts on.
        (
            InvoiceListFacts(outstanding=OWED, paid=Decimal("40.00")),
            date(2026, 9, 1),
            PaymentStatus.OVERDUE,
        ),
        # Nothing owed and a payment on record: paid.
        (InvoiceListFacts(last_paid_on=date(2026, 9, 10)), date(2026, 9, 1), PaymentStatus.PAID),
        # Nothing owed and no payment: credited in full or written off.
        (InvoiceListFacts(), date(2026, 9, 1), PaymentStatus.SETTLED),
    ],
)
def test_an_issued_invoice_is_classified_by_what_is_still_owed(
    facts: InvoiceListFacts, due: date, expected: PaymentStatus
) -> None:
    invoice = _invoice(
        uuid.uuid4(), uuid.uuid4(), status=InvoiceStatus.ISSUED, invoice_number=1, due_date=due
    )
    assert payment_status(invoice, facts, TODAY) is expected


def test_a_draft_is_a_draft_whatever_the_facts_say() -> None:
    draft = _invoice(uuid.uuid4(), uuid.uuid4(), due_date=date(2026, 1, 1))
    assert payment_status(draft, InvoiceListFacts(outstanding=OWED), TODAY) is PaymentStatus.DRAFT


def test_a_credit_note_is_not_reported_as_overdue() -> None:
    credit = _invoice(
        uuid.uuid4(),
        uuid.uuid4(),
        status=InvoiceStatus.ISSUED,
        invoice_number=2,
        due_date=date(2026, 1, 1),
        credits_invoice_id=uuid.uuid4(),
    )
    assert payment_status(credit, InvoiceListFacts(), TODAY) is PaymentStatus.CREDIT_NOTE


async def test_payment_date_appears_only_once_something_is_paid() -> None:
    repository = FakeListRepository()
    service, _, administration = _service(repository)
    org = uuid.uuid4()
    paid = _invoice(administration, org, status=InvoiceStatus.ISSUED, invoice_number=1)
    unpaid = _invoice(
        administration,
        org,
        status=InvoiceStatus.ISSUED,
        invoice_number=2,
        due_date=date(2026, 10, 1),
    )
    repository.facts[paid.id] = InvoiceListFacts(
        last_paid_on=date(2026, 9, 10), delivery_channel="email", delivery_status="sent"
    )
    repository.facts[unpaid.id] = InvoiceListFacts(outstanding=OWED)

    rows = await service.list_rows(
        administration_id=administration, invoices=[paid, unpaid], today=TODAY
    )

    assert rows[paid.id].payment_status is PaymentStatus.PAID
    assert rows[paid.id].payment_date == date(2026, 9, 10)
    assert (rows[paid.id].send_channel, rows[paid.id].send_status) == ("email", "sent")
    assert rows[unpaid.id].payment_status is PaymentStatus.OPEN
    assert rows[unpaid.id].payment_date is None
    assert rows[unpaid.id].send_channel is None
