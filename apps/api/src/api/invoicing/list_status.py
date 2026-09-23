"""What the sales invoice LIST says about each row beyond the invoice itself:
where it stands with the money, whether and how it was sent, and when it was
paid. Pure - the repository supplies the facts, this decides the words.

`SalesInvoice.status` is only draft/issued, because that is what the number
series and the books care about. A person scanning a list wants the
receivables state instead: is it overdue, is it paid. It is derived here from
`invoicing.receivable_items` (FR-AR-012's definition of "still owed") so the list
and the ageing report cannot disagree about what is overdue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from api.invoicing.model import InvoiceStatus, SalesInvoice


class PaymentStatus(StrEnum):
    DRAFT = "draft"
    CREDIT_NOTE = "credit_note"
    #: Issued, nothing paid, not yet due.
    OPEN = "open"
    #: Still owed and past its due date.
    OVERDUE = "overdue"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    #: Owes nothing but was not paid: credited in full or written off (FR-AR-013).
    SETTLED = "settled"


@dataclass(frozen=True, slots=True)
class InvoiceListFacts:
    """Raw facts about one invoice, as read from the database."""

    #: What is still owed. None means the invoice is not in `receivable_items`
    #: at all (draft, credit note, or nothing owed).
    outstanding: Decimal | None = None
    paid: Decimal = Decimal(0)
    #: The latest payment date that has not been voided.
    last_paid_on: date | None = None
    #: The most recent delivery attempt, if any.
    delivery_channel: str | None = None
    delivery_status: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceListRow:
    """What `_invoice_summary_json` adds to a list row."""

    gross_amount: Decimal | None
    payment_status: PaymentStatus
    payment_date: date | None
    send_channel: str | None
    send_status: str | None


def payment_status(invoice: SalesInvoice, facts: InvoiceListFacts, today: date) -> PaymentStatus:
    if invoice.status is InvoiceStatus.DRAFT:
        return PaymentStatus.DRAFT
    if invoice.credits_invoice_id is not None:
        return PaymentStatus.CREDIT_NOTE
    if facts.outstanding is not None and facts.outstanding > 0:
        if invoice.due_date is not None and invoice.due_date < today:
            return PaymentStatus.OVERDUE
        return PaymentStatus.PARTIALLY_PAID if facts.paid > 0 else PaymentStatus.OPEN
    return PaymentStatus.PAID if facts.last_paid_on is not None else PaymentStatus.SETTLED
