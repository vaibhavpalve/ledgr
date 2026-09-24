"""Value types for bank accounts, statement imports and reconciliation
(migrations 0065, 0070).

Reconciliation is not a second write path into the ledger: a match against a
sales invoice calls `api.invoicing.payments.SalesPaymentService.record()`,
and a generic match or a receipt's settlement calls `LedgerService.post()`
directly - the same two public entry points `api.assets.service` already uses
for depreciation and disposal.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


class BankAccountStatus(enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TransactionStatus(enum.Enum):
    UNMATCHED = "unmatched"
    RECONCILED = "reconciled"


class BankError(Exception):
    """Base for every refusal this module raises."""


class BankAccountNotFound(BankError):
    pass


class TransactionNotFound(BankError):
    pass


class TransactionAlreadyReconciled(BankError):
    pass


class NoOpenPeriod(BankError):
    def __init__(self, on: date) -> None:
        self.on = on
        super().__init__(f"{on} falls in no open period")


class NoActiveBankJournal(BankError):
    pass


class InvalidBankField(BankError):
    def __init__(self, field: str, message: str = "") -> None:
        self.field = field
        super().__init__(message or field)


@dataclass(frozen=True, slots=True)
class BankAccount:
    id: uuid.UUID
    administration_id: uuid.UUID
    name: str
    iban: str | None
    currency: str
    ledger_account_id: uuid.UUID
    status: BankAccountStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BankTransaction:
    id: uuid.UUID
    administration_id: uuid.UUID
    bank_account_id: uuid.UUID
    booking_date: date
    value_date: date | None
    amount: Decimal
    currency: str
    counterparty_name: str | None
    counterparty_iban: str | None
    description: str | None
    external_id: str
    status: TransactionStatus
    matched_sales_invoice_id: uuid.UUID | None
    matched_payment_id: uuid.UUID | None
    journal_entry_id: uuid.UUID | None
    reconciled_at: datetime | None
    matched_expense_id: uuid.UUID | None = None

    @property
    def is_inflow(self) -> bool:
        return self.amount > 0


@dataclass(frozen=True, slots=True)
class ImportResult:
    import_id: uuid.UUID
    transaction_count: int
    duplicate_count: int


class CandidateKind(enum.Enum):
    """What a bank line can settle: money IN pays a sales invoice, money OUT pays a receipt that
    was booked as paid from the business account or card (ADR-092)."""

    SALES_INVOICE = "sales_invoice"
    EXPENSE = "expense"


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """One open document whose amount equals a transaction's exactly - the suggestion the
    reconciliation screen offers, never applied without a click.

        document_id    the sales invoice's id, or the expense's
        reference      the invoice number: ours for a sales invoice, the supplier's for a receipt
        party_name     the customer, or the supplier
        amount         what is still open: the invoice's outstanding balance, the receipt's gross
        document_date  the invoice date, or the receipt's date
    """

    kind: CandidateKind
    document_id: uuid.UUID
    reference: str | None
    party_name: str
    amount: Decimal
    document_date: date


class ExpenseNotMatchable(BankError):
    """The receipt cannot settle this bank line: it is not posted, was not paid from the business
    account or card, is already matched, or its amount differs."""
