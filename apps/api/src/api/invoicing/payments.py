"""Payments received against a sales invoice - FR-AR-010's missing half.

    Dr  Bank / Kas (an asset account)    amount
    Cr  Debiteuren (AR control)          amount   <- carries the sub-ledger party

See migration 0052 for why this exists before dunning does: "overdue" means
"issued, past due AND NOT PAID", and until now nothing could say an invoice was
paid.

--- Through the ledger's own API, never around it ---

CLAUDE.md's first non-negotiable, and the same opening `api.invoicing.posting`
makes: this module builds an `EntryInput` and hands it to `LedgerService.post`.
It holds no SQL against `journal_entry` or `journal_line`.

--- Who may record one, and who may undo one ---

`post journal_entry` to record, `reverse journal_entry` to void - Appendix A's
own rows (ADR-012), NOT `create/send sales_invoice`. That is a deliberate
separation of duties: the Invoicer sends invoices and may not post, so the person
who raises an invoice cannot also be the person who records that cash arrived
against it. Letting them would hand out the classic lapping opportunity - take a
payment, never record it, and nothing anywhere disagrees.

--- A payment can never exceed what is owed ---

Overpayment, split allocation and one transaction covering many invoices are
FR-BNK-005 and arrive with bank reconciliation. Until then a payment above the
outstanding balance is refused rather than parked: silently absorbing somebody's
money into an account nobody reads is worse than asking for it to be recorded
properly. The refusal is checked here for a clear error and again by
`sales_invoice_payment_guard` under a row lock, which is what actually holds when
two payments arrive at once.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.invoicing.model import InvoiceNotFound, InvoicingError, NotAuthorizedToInvoice
from api.invoicing.posting import NoOpenPeriod
from api.ledger.model import ZERO, EntryInput, LineInput
from api.ledger.service import LedgerService

__all__ = [
    "BalanceState",
    "InvoiceBalance",
    "InvoicePayment",
    "PayableInvoice",
    "PaymentMethod",
    "PaymentRepository",
    "SalesPaymentService",
    "InvoiceNotPayable",
    "InvalidPaymentAmount",
    "PaymentExceedsOutstanding",
    "NoPaymentJournal",
    "PaymentNotFound",
    "PaymentAlreadyVoided",
    "PaymentPostingUnavailable",
]

#: Appendix A rows, reused rather than invented (ADR-012).
RECORD_PAYMENT = ("post", "journal_entry")
VOID_PAYMENT = ("reverse", "journal_entry")
#: Reading what is owed is part of working on the invoice.
VIEW_BALANCE = ("create", "sales_invoice")

_CENT = Decimal("0.01")


class PaymentMethod(enum.Enum):
    """How the money arrived. Chooses the journal; does not change the entry."""

    BANK_TRANSFER = "bank_transfer"
    CASH = "cash"
    CARD = "card"
    OTHER = "other"

    @property
    def journal_type(self) -> str:
        return "cash" if self is PaymentMethod.CASH else "bank"


class BalanceState(enum.Enum):
    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    #: Owed nothing because it was credited, and never paid.
    CREDITED = "credited"


@dataclass(frozen=True, slots=True)
class InvoiceBalance:
    """What one issued invoice owes. All amounts `Decimal`, two places (NFR-031)."""

    invoice_id: uuid.UUID
    gross: Decimal
    credited: Decimal
    paid: Decimal

    @property
    def outstanding(self) -> Decimal:
        return self.gross - self.credited - self.paid

    @property
    def state(self) -> BalanceState:
        if self.outstanding > 0:
            if self.paid > 0 or self.credited > 0:
                return BalanceState.PARTIALLY_PAID
            return BalanceState.OPEN
        return BalanceState.PAID if self.paid > 0 else BalanceState.CREDITED


@dataclass(frozen=True, slots=True)
class PayableInvoice:
    """The facts about an invoice a payment needs, and nothing more."""

    id: uuid.UUID
    administration_id: uuid.UUID
    is_issued: bool
    is_credit_note: bool
    invoice_reference: str | None
    customer_name: str


@dataclass(frozen=True, slots=True)
class InvoicePayment:
    id: uuid.UUID
    administration_id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    paid_on: date
    method: PaymentMethod
    reference: str | None
    bank_account_id: uuid.UUID
    journal_entry_id: uuid.UUID
    recorded_by_user_id: uuid.UUID
    recorded_at: datetime
    voided_at: datetime | None = None
    void_journal_entry_id: uuid.UUID | None = None

    @property
    def is_voided(self) -> bool:
        return self.voided_at is not None


class InvoiceNotPayable(InvoicingError):
    """A draft owes nothing and a credit note is not a debt."""


class InvalidPaymentAmount(InvoicingError):
    """Not positive, or finer than a cent. Refused rather than rounded: a
    payment is a fact about money that moved, and rounding it would record a
    different fact."""


class PaymentExceedsOutstanding(InvoicingError):
    def __init__(self, outstanding: Decimal) -> None:
        self.outstanding = outstanding
        super().__init__(f"only {outstanding} is outstanding")


class NoPaymentJournal(InvoicingError):
    """FR-GL-002's bank or cash journal is missing or ambiguous."""

    def __init__(self, journal_type: str) -> None:
        self.journal_type = journal_type
        super().__init__(f"no single active {journal_type} journal to post a receipt into")


class PaymentPostingUnavailable(InvoicingError):
    """The books cannot take this receipt: no AR control account, or no debtor
    on the invoice's entry. Names what is missing rather than guessing."""

    def __init__(self, missing: str, detail: str) -> None:
        self.missing = missing
        super().__init__(detail)


class PaymentNotFound(InvoicingError):
    pass


class PaymentAlreadyVoided(InvoicingError):
    pass


class PaymentRepository(Protocol):
    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def invoice(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> PayableInvoice | None: ...

    async def balance_of(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> InvoiceBalance | None: ...

    async def journal_of_type(
        self, *, administration_id: uuid.UUID, journal_type: str
    ) -> uuid.UUID | None:
        """The one active journal of this type, or None for none OR several -
        picking one of two arbitrarily would split FR-GL-013's numbering."""
        ...

    async def open_period_for(
        self, *, administration_id: uuid.UUID, on: date
    ) -> uuid.UUID | None: ...

    async def receivable_control_account(
        self, *, administration_id: uuid.UUID
    ) -> uuid.UUID | None: ...

    async def invoice_party(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> uuid.UUID | None:
        """The debtor the invoice was posted against (FR-GL-006)."""
        ...

    async def insert_payment(self, payment: InvoicePayment) -> InvoicePayment: ...

    async def get_payment(
        self, *, administration_id: uuid.UUID, payment_id: uuid.UUID
    ) -> InvoicePayment | None: ...

    async def payments_for(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Sequence[InvoicePayment]: ...

    async def mark_voided(
        self,
        *,
        administration_id: uuid.UUID,
        payment_id: uuid.UUID,
        user_id: uuid.UUID,
        void_journal_entry_id: uuid.UUID,
    ) -> InvoicePayment: ...


def normalise_amount(amount: Decimal) -> Decimal:
    """`amount` as a positive two-place Decimal, or `InvalidPaymentAmount`.

    Refuses a float outright (NFR-031) and anything finer than a cent, and does
    not round either.
    """
    if isinstance(amount, float) or not isinstance(amount, Decimal):
        raise InvalidPaymentAmount("a payment amount must be a Decimal, not a float (NFR-031)")
    if not amount.is_finite() or amount <= 0:
        raise InvalidPaymentAmount("a payment amount must be greater than zero")
    if amount != amount.quantize(_CENT):
        raise InvalidPaymentAmount("a payment amount has at most two decimal places")
    return amount.quantize(_CENT)


class SalesPaymentService:
    def __init__(
        self,
        repository: PaymentRepository,
        ledger: LedgerService,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._ledger = ledger
        self._authorization = authorization
        self._audit = audit_log

    # -- record ---------------------------------------------------------------

    async def record(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        amount: Decimal,
        paid_on: date,
        method: PaymentMethod,
        bank_account_id: uuid.UUID,
        reference: str | None = None,
        correlation_id: str | None = None,
    ) -> InvoicePayment:
        await self._require(RECORD_PAYMENT, actor_user_id, administration_id)
        amount = normalise_amount(amount)

        invoice = await self._invoice_or_refuse(administration_id, invoice_id)
        if not invoice.is_issued or invoice.is_credit_note:
            raise InvoiceNotPayable(
                f"invoice {invoice.invoice_reference or invoice.id} is not an issued invoice "
                f"that is owed money"
            )

        balance = await self._repository.balance_of(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if balance is None:  # pragma: no cover - `invoice` above already found it
            raise InvoiceNotFound(f"sales invoice {invoice_id} not found")
        if amount > balance.outstanding:
            raise PaymentExceedsOutstanding(balance.outstanding)

        # Everything that can fail on configuration, before anything is written.
        journal_type = method.journal_type
        journal_id = await self._repository.journal_of_type(
            administration_id=administration_id, journal_type=journal_type
        )
        if journal_id is None:
            raise NoPaymentJournal(journal_type)

        period_id = await self._repository.open_period_for(
            administration_id=administration_id, on=paid_on
        )
        if period_id is None:
            # Refused rather than moved to a period that is open, for the reason
            # `posting.NoOpenPeriod` gives: a receipt belongs to the period it
            # arrived in.
            raise NoOpenPeriod(f"{paid_on} falls in no open period")

        receivable = await self._repository.receivable_control_account(
            administration_id=administration_id
        )
        if receivable is None:
            raise PaymentPostingUnavailable(
                "accounts_receivable",
                "this administration's chart has no accounts-receivable control account",
            )
        party_id = await self._repository.invoice_party(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if party_id is None:
            raise PaymentPostingUnavailable(
                "debtor",
                f"invoice {invoice.invoice_reference} has no debtor in the sub-ledger, so "
                f"there is no party to credit",
            )

        payment_id = uuid.uuid4()
        entry = await self._ledger.post(
            EntryInput(
                administration_id=administration_id,
                journal_id=journal_id,
                period_id=period_id,
                entry_date=paid_on,
                description=_description(invoice),
                lines=[
                    LineInput(
                        account_id=bank_account_id,
                        debit=amount,
                        credit=ZERO,
                        description=_description(invoice),
                    ),
                    LineInput(
                        account_id=receivable,
                        debit=ZERO,
                        credit=amount,
                        # FR-GL-006: mandatory on a control account.
                        subledger_party_id=party_id,
                        description=_description(invoice),
                    ),
                ],
                document_reference=invoice.invoice_reference,
                posted_by_user_id=actor_user_id,
                source_system="invoicing",
                # NFR-032: this payment's own id. A retry of the same request
                # carries the same Idempotency-Key and never reaches here twice.
                idempotency_key=f"sales_invoice_payment:{payment_id}",
            ),
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
        )

        payment = await self._repository.insert_payment(
            InvoicePayment(
                id=payment_id,
                administration_id=administration_id,
                invoice_id=invoice_id,
                amount=amount,
                paid_on=paid_on,
                method=method,
                reference=(reference.strip() or None) if reference else None,
                bank_account_id=bank_account_id,
                journal_entry_id=entry.id,
                recorded_by_user_id=actor_user_id,
                recorded_at=datetime.now().astimezone(),
            )
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="record_sales_invoice_payment",
            resource_id=payment.id,
            correlation_id=correlation_id,
            detail={
                "invoice_id": str(invoice_id),
                "amount": str(amount),
                "method": method.value,
                "paid_on": paid_on.isoformat(),
                "journal_entry_id": str(entry.id),
            },
        )
        return payment

    # -- void -----------------------------------------------------------------

    async def void(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        payment_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        void_date: date,
        correlation_id: str | None = None,
    ) -> InvoicePayment:
        """Undo a payment by reversing its entry (FR-GL-003) - never by editing
        or deleting it. The invoice is owed again from the moment this returns.

        The reversal is dated `void_date`, not the payment's date: the payment's
        period is usually closed by the time the mistake is found, and posting the
        correction into the current period is the correct treatment.
        """
        await self._require(VOID_PAYMENT, actor_user_id, administration_id)

        payment = await self._repository.get_payment(
            administration_id=administration_id, payment_id=payment_id
        )
        if payment is None or payment.invoice_id != invoice_id:
            raise PaymentNotFound(f"payment {payment_id} not found on invoice {invoice_id}")
        if payment.is_voided:
            raise PaymentAlreadyVoided(f"payment {payment_id} was already voided")

        period_id = await self._repository.open_period_for(
            administration_id=administration_id, on=void_date
        )
        if period_id is None:
            raise NoOpenPeriod(f"{void_date} falls in no open period")

        reversal = await self._ledger.reverse(
            entry_id=payment.journal_entry_id,
            period_id=period_id,
            entry_date=void_date,
            description=f"Betaling ongedaan gemaakt ({payment.amount})",
            actor_user_id=actor_user_id,
            source_system="invoicing",
            idempotency_key=f"sales_invoice_payment_void:{payment.id}",
            correlation_id=correlation_id,
        )
        voided = await self._repository.mark_voided(
            administration_id=administration_id,
            payment_id=payment.id,
            user_id=actor_user_id,
            void_journal_entry_id=reversal.id,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="void_sales_invoice_payment",
            resource_id=payment.id,
            correlation_id=correlation_id,
            detail={
                "invoice_id": str(invoice_id),
                "amount": str(payment.amount),
                "reversal_entry_id": str(reversal.id),
            },
        )
        return voided

    # -- reading --------------------------------------------------------------

    async def payments(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Sequence[InvoicePayment]:
        await self._require(VIEW_BALANCE, actor_user_id, administration_id)
        await self._invoice_or_refuse(administration_id, invoice_id)
        return await self._repository.payments_for(
            administration_id=administration_id, invoice_id=invoice_id
        )

    async def balance(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> InvoiceBalance:
        await self._require(VIEW_BALANCE, actor_user_id, administration_id)
        await self._invoice_or_refuse(administration_id, invoice_id)
        balance = await self._repository.balance_of(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if balance is None:
            # Exists but is not an issued, non-credit-note invoice: nothing owed.
            raise InvoiceNotPayable(f"invoice {invoice_id} owes nothing")
        return balance

    # -- internals ------------------------------------------------------------

    async def _invoice_or_refuse(
        self, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> PayableInvoice:
        invoice = await self._repository.invoice(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if invoice is None:
            raise InvoiceNotFound(f"sales invoice {invoice_id} not found")
        return invoice

    async def _require(
        self, permission: tuple[str, str], user_id: uuid.UUID, administration_id: uuid.UUID
    ) -> None:
        action, resource_type = permission
        decision = await self._authorization.authorize(
            AuthorizationRequest(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                target=AdministrationScope(administration_id),
                attributes=ResourceAttributes(),
            )
        )
        if not decision.allowed:
            await self._record(
                administration_id=administration_id,
                user_id=user_id,
                action=f"{action}_{resource_type}",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise NotAuthorizedToInvoice(action, resource_type, decision.detail or decision.reason)

    async def _record(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        resource_id: uuid.UUID,
        detail: dict[str, Any],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        correlation_id: str | None = None,
    ) -> None:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise InvoiceNotFound(f"administration {administration_id} does not exist")
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="sales_invoice_payment",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )


def _description(invoice: PayableInvoice) -> str:
    """FR-GL-004 requires one. The reference and the customer identify the
    document, as they do on the invoice's own entry."""
    return f"Betaling {invoice.invoice_reference or invoice.id} - {invoice.customer_name}"
