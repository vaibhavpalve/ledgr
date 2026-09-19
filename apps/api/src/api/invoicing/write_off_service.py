"""Writing off an uncollectable invoice - FR-AR-013, SI-10 (ADR-076).

    Write-off         Dr  Afschrijving oninbare vorderingen   outstanding
                      Cr  Debiteuren (AR control)             outstanding   <- party
    VAT reclaim       Dr  Te betalen omzetbelasting (per treatment)   the VAT part
                      Cr  Afschrijving oninbare vorderingen           the same

Both entries go through `LedgerService`, never around it (CLAUDE.md rule 1). See migration
0058 for why the reclaim is a second entry that may come later, and `bad_debt` for how the
VAT part is worked out and when it may be claimed.

--- Who may do it ---

`post journal_entry` to write off or reclaim, `reverse journal_entry` to void - Appendix A's
own rows, exactly as payments use them (ADR-070), and for the same reason: the person who
raises invoices must not be the one who makes a debt disappear. Writing a receivable off
without anybody having paid is precisely the entry a lapping scheme wants.

--- A write-off is the WHOLE outstanding balance ---

Never part of it: a partial write-off would leave a receivable on the books that nobody
intends to collect. A customer who then pays anyway is handled by voiding the write-off,
which reverses the entries and owes the invoice again, VAT included.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.invoicing.bad_debt import (
    GroupShare,
    VatGroupTotal,
    split_outstanding,
    vat_reclaim_eligible_on,
)
from api.invoicing.model import InvoiceNotFound, InvoicingError, NotAuthorizedToInvoice
from api.invoicing.payments import InvoiceBalance
from api.invoicing.posting import NoOpenPeriod
from api.ledger.model import ZERO, EntryInput, LineInput
from api.ledger.service import LedgerService

__all__ = [
    "InvoiceWriteOff",
    "VatSplitLine",
    "WriteOffInvoice",
    "WriteOffRepository",
    "WriteOffService",
    "WriteOffNotAllowed",
    "NothingOutstanding",
    "WriteOffReasonMissing",
    "WriteOffDateInFuture",
    "NoWriteOffJournal",
    "WriteOffPostingUnavailable",
    "ExpenseAccountInvalid",
    "WriteOffNotFound",
    "WriteOffAlreadyVoided",
    "VatAlreadyReclaimed",
    "NothingToReclaim",
    "VatReclaimNotYetAllowed",
    "NoOpenPeriod",
]

WRITE_OFF = ("post", "journal_entry")
VOID_WRITE_OFF = ("reverse", "journal_entry")
VIEW_WRITE_OFFS = ("create", "sales_invoice")

#: `sales_posting_account.purpose` for the output-VAT account (0040).
VAT_OUTPUT = "vat_output"


@dataclass(frozen=True, slots=True)
class VatSplitLine:
    treatment: str
    vat: Decimal


@dataclass(frozen=True, slots=True)
class WriteOffInvoice:
    id: uuid.UUID
    administration_id: uuid.UUID
    is_issued: bool
    is_credit_note: bool
    invoice_reference: str | None
    customer_name: str
    invoice_date: date
    due_date: date | None


@dataclass(frozen=True, slots=True)
class InvoiceWriteOff:
    id: uuid.UUID
    administration_id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    vat_amount: Decimal
    vat_split: tuple[VatSplitLine, ...]
    written_off_on: date
    reason: str
    customer_insolvent: bool
    expense_account_id: uuid.UUID
    journal_entry_id: uuid.UUID
    recorded_by_user_id: uuid.UUID
    recorded_at: datetime
    vat_reclaimed_on: date | None = None
    vat_reclaim_journal_entry_id: uuid.UUID | None = None
    voided_at: datetime | None = None
    void_journal_entry_id: uuid.UUID | None = None
    void_reclaim_journal_entry_id: uuid.UUID | None = None

    @property
    def is_voided(self) -> bool:
        return self.voided_at is not None

    @property
    def is_vat_reclaimed(self) -> bool:
        return self.vat_reclaim_journal_entry_id is not None


class WriteOffNotAllowed(InvoicingError):
    """A draft owes nothing and a credit note is not a debt."""


class NothingOutstanding(InvoicingError):
    """Paid, credited or already written off: no balance left to write off."""


class WriteOffReasonMissing(InvoicingError):
    """A write-off is a decision somebody may have to account for; it needs a reason."""


class WriteOffDateInFuture(InvoicingError):
    pass


class NoWriteOffJournal(InvoicingError):
    """FR-GL-002's memorial journal is missing or ambiguous."""


class WriteOffPostingUnavailable(InvoicingError):
    def __init__(self, missing: str, detail: str) -> None:
        self.missing = missing
        super().__init__(detail)


class ExpenseAccountInvalid(InvoicingError):
    """The account a write-off is booked to must be this administration's expense account."""


class WriteOffNotFound(InvoicingError):
    pass


class WriteOffAlreadyVoided(InvoicingError):
    pass


class VatAlreadyReclaimed(InvoicingError):
    pass


class NothingToReclaim(InvoicingError):
    """The written-off balance carries no VAT (zero-rated, exempt, reverse-charged)."""


class VatReclaimNotYetAllowed(InvoicingError):
    def __init__(self, eligible_on: date) -> None:
        self.eligible_on = eligible_on
        super().__init__(f"the VAT may be reclaimed from {eligible_on}")


class WriteOffRepository(Protocol):
    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def invoice(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> WriteOffInvoice | None: ...

    async def balance_of(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> InvoiceBalance | None: ...

    async def vat_groups(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Sequence[VatGroupTotal]: ...

    async def is_expense_account(
        self, *, administration_id: uuid.UUID, account_id: uuid.UUID
    ) -> bool: ...

    async def journal_of_type(
        self, *, administration_id: uuid.UUID, journal_type: str
    ) -> uuid.UUID | None: ...

    async def open_period_for(
        self, *, administration_id: uuid.UUID, on: date
    ) -> uuid.UUID | None: ...

    async def receivable_control_account(
        self, *, administration_id: uuid.UUID
    ) -> uuid.UUID | None: ...

    async def invoice_party(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> uuid.UUID | None: ...

    async def vat_output_account(
        self, *, administration_id: uuid.UUID, vat_treatment: str
    ) -> uuid.UUID | None: ...

    async def insert_write_off(self, write_off: InvoiceWriteOff) -> InvoiceWriteOff: ...

    async def get_write_off(
        self, *, administration_id: uuid.UUID, write_off_id: uuid.UUID
    ) -> InvoiceWriteOff | None: ...

    async def write_offs_for(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Sequence[InvoiceWriteOff]: ...

    async def mark_vat_reclaimed(
        self,
        *,
        administration_id: uuid.UUID,
        write_off_id: uuid.UUID,
        user_id: uuid.UUID,
        reclaimed_on: date,
        journal_entry_id: uuid.UUID,
    ) -> InvoiceWriteOff: ...

    async def mark_voided(
        self,
        *,
        administration_id: uuid.UUID,
        write_off_id: uuid.UUID,
        user_id: uuid.UUID,
        void_journal_entry_id: uuid.UUID,
        void_reclaim_journal_entry_id: uuid.UUID | None,
    ) -> InvoiceWriteOff: ...


class WriteOffService:
    def __init__(
        self,
        repository: WriteOffRepository,
        ledger: LedgerService,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._ledger = ledger
        self._authorization = authorization
        self._audit = audit_log

    # -- write off --------------------------------------------------------------------

    async def write_off(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        expense_account_id: uuid.UUID,
        written_off_on: date,
        today: date,
        reason: str,
        customer_insolvent: bool = False,
        reclaim_vat: bool = False,
        correlation_id: str | None = None,
    ) -> InvoiceWriteOff:
        """Write the whole outstanding balance off; optionally reclaim its VAT in the same
        transaction when the rules already allow it (otherwise: refused, nothing written)."""
        await self._require(WRITE_OFF, actor_user_id, administration_id)

        reason = (reason or "").strip()
        if not reason:
            raise WriteOffReasonMissing("a write-off needs a reason")
        if written_off_on > today:
            raise WriteOffDateInFuture(f"{written_off_on} is in the future")

        invoice = await self._invoice_or_refuse(administration_id, invoice_id)
        if not invoice.is_issued or invoice.is_credit_note:
            raise WriteOffNotAllowed(
                f"invoice {invoice.invoice_reference or invoice.id} is not an issued invoice "
                f"that is owed money"
            )
        balance = await self._repository.balance_of(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if balance is None or balance.outstanding <= 0:
            raise NothingOutstanding(f"invoice {invoice.invoice_reference} owes nothing")

        groups = await self._repository.vat_groups(
            administration_id=administration_id, invoice_id=invoice_id
        )
        shares = split_outstanding(groups, balance.outstanding)
        vat_total = sum((share.vat for share in shares), ZERO)

        if reclaim_vat:
            if vat_total <= 0:
                raise NothingToReclaim("this balance carries no VAT")
            self._check_eligible(invoice, customer_insolvent, on=written_off_on)

        if not await self._repository.is_expense_account(
            administration_id=administration_id, account_id=expense_account_id
        ):
            raise ExpenseAccountInvalid(f"account {expense_account_id} is not an expense account")

        # Everything that can fail on configuration, before anything is written.
        journal_id = await self._repository.journal_of_type(
            administration_id=administration_id, journal_type="memorial"
        )
        if journal_id is None:
            raise NoWriteOffJournal("no single active memorial journal to post a write-off into")
        period_id = await self._repository.open_period_for(
            administration_id=administration_id, on=written_off_on
        )
        if period_id is None:
            raise NoOpenPeriod(f"{written_off_on} falls in no open period")
        receivable = await self._repository.receivable_control_account(
            administration_id=administration_id
        )
        if receivable is None:
            raise WriteOffPostingUnavailable(
                "accounts_receivable",
                "this administration's chart has no accounts-receivable control account",
            )
        party_id = await self._repository.invoice_party(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if party_id is None:
            raise WriteOffPostingUnavailable(
                "debtor",
                f"invoice {invoice.invoice_reference} has no debtor in the sub-ledger",
            )
        vat_accounts = await self._vat_accounts(administration_id, shares) if reclaim_vat else {}

        write_off_id = uuid.uuid4()
        amount = balance.outstanding
        description = _description(invoice)
        entry = await self._ledger.post(
            EntryInput(
                administration_id=administration_id,
                journal_id=journal_id,
                period_id=period_id,
                entry_date=written_off_on,
                description=description,
                lines=[
                    LineInput(
                        account_id=expense_account_id,
                        debit=amount,
                        credit=ZERO,
                        description=description,
                    ),
                    LineInput(
                        account_id=receivable,
                        debit=ZERO,
                        credit=amount,
                        # FR-GL-006: mandatory on a control account.
                        subledger_party_id=party_id,
                        description=description,
                    ),
                ],
                document_reference=invoice.invoice_reference,
                posted_by_user_id=actor_user_id,
                source_system="invoicing",
                idempotency_key=f"sales_invoice_write_off:{write_off_id}",
            ),
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
        )

        written = await self._repository.insert_write_off(
            InvoiceWriteOff(
                id=write_off_id,
                administration_id=administration_id,
                invoice_id=invoice_id,
                amount=amount,
                vat_amount=vat_total,
                vat_split=tuple(
                    VatSplitLine(share.treatment, share.vat) for share in shares if share.vat > 0
                ),
                written_off_on=written_off_on,
                reason=reason,
                customer_insolvent=customer_insolvent,
                expense_account_id=expense_account_id,
                journal_entry_id=entry.id,
                recorded_by_user_id=actor_user_id,
                recorded_at=datetime.now().astimezone(),
            )
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="write_off_sales_invoice",
            resource_id=written.id,
            correlation_id=correlation_id,
            detail={
                "invoice_id": str(invoice_id),
                "amount": str(amount),
                "vat_amount": str(vat_total),
                "customer_insolvent": customer_insolvent,
                "journal_entry_id": str(entry.id),
            },
        )
        if reclaim_vat:
            written = await self._post_reclaim(
                written,
                invoice=invoice,
                journal_id=journal_id,
                period_id=period_id,
                reclaim_date=written_off_on,
                vat_accounts=vat_accounts,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
            )
        return written

    # -- reclaim the VAT ----------------------------------------------------------------

    async def reclaim_vat(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        write_off_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
        correlation_id: str | None = None,
    ) -> InvoiceWriteOff:
        """Claim back the VAT of an earlier write-off, once the waiting period has passed
        (or at once if the customer was recorded as insolvent)."""
        await self._require(WRITE_OFF, actor_user_id, administration_id)
        write_off = await self._write_off_or_refuse(administration_id, invoice_id, write_off_id)
        if write_off.is_voided:
            raise WriteOffAlreadyVoided(f"write-off {write_off_id} was voided")
        if write_off.is_vat_reclaimed:
            raise VatAlreadyReclaimed(f"write-off {write_off_id} already reclaimed its VAT")
        if write_off.vat_amount <= 0:
            raise NothingToReclaim("this write-off carries no VAT")

        invoice = await self._invoice_or_refuse(administration_id, invoice_id)
        self._check_eligible(invoice, write_off.customer_insolvent, on=today)

        journal_id = await self._repository.journal_of_type(
            administration_id=administration_id, journal_type="memorial"
        )
        if journal_id is None:
            raise NoWriteOffJournal("no single active memorial journal to post a reclaim into")
        period_id = await self._repository.open_period_for(
            administration_id=administration_id, on=today
        )
        if period_id is None:
            raise NoOpenPeriod(f"{today} falls in no open period")
        accounts = await self._vat_accounts_for(administration_id, write_off)
        return await self._post_reclaim(
            write_off,
            invoice=invoice,
            journal_id=journal_id,
            period_id=period_id,
            reclaim_date=today,
            vat_accounts=accounts,
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
        )

    # -- void -------------------------------------------------------------------------------

    async def void(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        write_off_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        void_date: date,
        correlation_id: str | None = None,
    ) -> InvoiceWriteOff:
        """Undo a write-off - and its VAT reclaim, if there was one - by reversing the
        entries (FR-GL-003). The invoice is owed again from the moment this returns."""
        await self._require(VOID_WRITE_OFF, actor_user_id, administration_id)
        write_off = await self._write_off_or_refuse(administration_id, invoice_id, write_off_id)
        if write_off.is_voided:
            raise WriteOffAlreadyVoided(f"write-off {write_off_id} was already voided")

        period_id = await self._repository.open_period_for(
            administration_id=administration_id, on=void_date
        )
        if period_id is None:
            raise NoOpenPeriod(f"{void_date} falls in no open period")

        void_reclaim_id: uuid.UUID | None = None
        if write_off.vat_reclaim_journal_entry_id is not None:
            # The VAT reclaim first: it was booked second, so it is undone first. In a later
            # period this is the VAT being paid back, which is what happens when the customer
            # pays after all.
            reclaim_reversal = await self._ledger.reverse(
                entry_id=write_off.vat_reclaim_journal_entry_id,
                period_id=period_id,
                entry_date=void_date,
                description=f"BTW-teruggaaf oninbaar ongedaan gemaakt ({write_off.vat_amount})",
                actor_user_id=actor_user_id,
                source_system="invoicing",
                idempotency_key=f"sales_invoice_write_off_vat_void:{write_off.id}",
                correlation_id=correlation_id,
            )
            void_reclaim_id = reclaim_reversal.id
        reversal = await self._ledger.reverse(
            entry_id=write_off.journal_entry_id,
            period_id=period_id,
            entry_date=void_date,
            description=f"Afschrijving oninbaar ongedaan gemaakt ({write_off.amount})",
            actor_user_id=actor_user_id,
            source_system="invoicing",
            idempotency_key=f"sales_invoice_write_off_void:{write_off.id}",
            correlation_id=correlation_id,
        )
        voided = await self._repository.mark_voided(
            administration_id=administration_id,
            write_off_id=write_off.id,
            user_id=actor_user_id,
            void_journal_entry_id=reversal.id,
            void_reclaim_journal_entry_id=void_reclaim_id,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="void_sales_invoice_write_off",
            resource_id=write_off.id,
            correlation_id=correlation_id,
            detail={
                "invoice_id": str(invoice_id),
                "amount": str(write_off.amount),
                "reversal_entry_id": str(reversal.id),
                "vat_reclaim_reversed": void_reclaim_id is not None,
            },
        )
        return voided

    # -- reading ------------------------------------------------------------------------------

    async def write_offs(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Sequence[InvoiceWriteOff]:
        await self._require(VIEW_WRITE_OFFS, actor_user_id, administration_id)
        await self._invoice_or_refuse(administration_id, invoice_id)
        return await self._repository.write_offs_for(
            administration_id=administration_id, invoice_id=invoice_id
        )

    async def balance(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> InvoiceBalance:
        await self._require(VIEW_WRITE_OFFS, actor_user_id, administration_id)
        await self._invoice_or_refuse(administration_id, invoice_id)
        balance = await self._repository.balance_of(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if balance is None:
            raise WriteOffNotAllowed(f"invoice {invoice_id} owes nothing")
        return balance

    # -- internals ----------------------------------------------------------------------------

    def _check_eligible(self, invoice: WriteOffInvoice, insolvent: bool, *, on: date) -> None:
        eligible_on = vat_reclaim_eligible_on(
            due_date=invoice.due_date,
            invoice_date=invoice.invoice_date,
            customer_insolvent=insolvent,
        )
        if eligible_on is not None and on < eligible_on:
            raise VatReclaimNotYetAllowed(eligible_on)

    async def _vat_accounts(
        self, administration_id: uuid.UUID, shares: Sequence[GroupShare]
    ) -> dict[str, uuid.UUID]:
        return await self._resolve_vat_accounts(
            administration_id, [share.treatment for share in shares if share.vat > 0]
        )

    async def _vat_accounts_for(
        self, administration_id: uuid.UUID, write_off: InvoiceWriteOff
    ) -> dict[str, uuid.UUID]:
        return await self._resolve_vat_accounts(
            administration_id, [line.treatment for line in write_off.vat_split]
        )

    async def _resolve_vat_accounts(
        self, administration_id: uuid.UUID, treatments: Sequence[str]
    ) -> dict[str, uuid.UUID]:
        accounts: dict[str, uuid.UUID] = {}
        for treatment in treatments:
            account = await self._repository.vat_output_account(
                administration_id=administration_id, vat_treatment=treatment
            )
            if account is None:
                raise WriteOffPostingUnavailable(
                    VAT_OUTPUT,
                    f"no output-VAT account is mapped for the VAT treatment {treatment!r}, so "
                    f"the VAT cannot be reclaimed; VAT in the wrong account is VAT in the "
                    f"wrong rubriek",
                )
            accounts[treatment] = account
        return accounts

    async def _post_reclaim(
        self,
        write_off: InvoiceWriteOff,
        *,
        invoice: WriteOffInvoice,
        journal_id: uuid.UUID,
        period_id: uuid.UUID,
        reclaim_date: date,
        vat_accounts: dict[str, uuid.UUID],
        actor_user_id: uuid.UUID,
        correlation_id: str | None,
    ) -> InvoiceWriteOff:
        description = f"BTW-teruggaaf oninbaar {invoice.invoice_reference or invoice.id}"
        lines = [
            LineInput(
                account_id=vat_accounts[line.treatment],
                debit=line.vat,
                credit=ZERO,
                description=description,
                # FR-VAT-001: the treatment names which rubriek this correction is reported in.
                vat_treatment=line.treatment,
            )
            for line in write_off.vat_split
        ]
        lines.append(
            LineInput(
                account_id=write_off.expense_account_id,
                debit=ZERO,
                credit=write_off.vat_amount,
                description=description,
            )
        )
        entry = await self._ledger.post(
            EntryInput(
                administration_id=write_off.administration_id,
                journal_id=journal_id,
                period_id=period_id,
                entry_date=reclaim_date,
                description=description,
                lines=lines,
                document_reference=invoice.invoice_reference,
                posted_by_user_id=actor_user_id,
                source_system="invoicing",
                idempotency_key=f"sales_invoice_write_off_vat:{write_off.id}",
            ),
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
        )
        updated = await self._repository.mark_vat_reclaimed(
            administration_id=write_off.administration_id,
            write_off_id=write_off.id,
            user_id=actor_user_id,
            reclaimed_on=reclaim_date,
            journal_entry_id=entry.id,
        )
        await self._record(
            administration_id=write_off.administration_id,
            user_id=actor_user_id,
            action="reclaim_write_off_vat",
            resource_id=write_off.id,
            correlation_id=correlation_id,
            detail={
                "invoice_id": str(write_off.invoice_id),
                "vat_amount": str(write_off.vat_amount),
                "journal_entry_id": str(entry.id),
            },
        )
        return updated

    async def _invoice_or_refuse(
        self, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> WriteOffInvoice:
        invoice = await self._repository.invoice(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if invoice is None:
            raise InvoiceNotFound(f"sales invoice {invoice_id} not found")
        return invoice

    async def _write_off_or_refuse(
        self, administration_id: uuid.UUID, invoice_id: uuid.UUID, write_off_id: uuid.UUID
    ) -> InvoiceWriteOff:
        write_off = await self._repository.get_write_off(
            administration_id=administration_id, write_off_id=write_off_id
        )
        if write_off is None or write_off.invoice_id != invoice_id:
            raise WriteOffNotFound(f"write-off {write_off_id} not found on invoice {invoice_id}")
        return write_off

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
                resource_type="sales_invoice_write_off",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )


def _description(invoice: WriteOffInvoice) -> str:
    """FR-GL-004 requires one."""
    return (
        f"Afschrijving oninbaar {invoice.invoice_reference or invoice.id} - {invoice.customer_name}"
    )
