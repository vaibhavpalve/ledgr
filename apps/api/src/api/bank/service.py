"""Bank accounts, statement import and reconciliation.

Two ways a transaction is reconciled, and both are EXISTING public entry
points, never a new write path into the ledger (CLAUDE.md non-negotiable #1):

    matched to an invoice   api.invoicing.payments.SalesPaymentService.record()
    matched to anything else (an expense paid, a transfer, a fee)
                             LedgerService.post() directly, the same way
                             api.assets.service posts depreciation and disposal
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.bank.csv_parser import StatementRow, parse_statement_csv
from api.bank.model import (
    BankAccount,
    BankAccountNotFound,
    BankTransaction,
    ImportResult,
    InvalidBankField,
    MatchCandidate,
    NoActiveBankJournal,
    NoOpenPeriod,
    TransactionAlreadyReconciled,
    TransactionNotFound,
    TransactionStatus,
)
from api.bank.repository import SqlBankRepository
from api.invoicing.model import InvoicingError
from api.invoicing.payments import PaymentMethod, SalesPaymentService
from api.ledger.model import EntryInput, LineInput
from api.ledger.service import LedgerService


class BankService:
    def __init__(
        self,
        repository: SqlBankRepository,
        ledger: LedgerService,
        payments: SalesPaymentService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._ledger = ledger
        self._payments = payments
        self._audit = audit_log

    # -- accounts -------------------------------------------------------------

    async def create_account(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        name: str,
        iban: str | None,
        currency: str,
        ledger_account_id: uuid.UUID,
    ) -> BankAccount:
        if not name.strip():
            raise InvalidBankField("name", "a bank account needs a name")
        account_type = await self._repository.account_type_of(
            administration_id=administration_id, ledger_account_id=ledger_account_id
        )
        if account_type is None:
            raise InvalidBankField("ledger_account_id", "that account does not exist")
        if account_type != "asset":
            raise InvalidBankField(
                "ledger_account_id", f"account is {account_type}, expected asset"
            )

        account = await self._repository.create_account(
            organization_id=organization_id,
            administration_id=administration_id,
            name=name,
            iban=iban,
            currency=currency.upper(),
            ledger_account_id=ledger_account_id,
            user_id=actor_user_id,
        )
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action="create_bank_account",
                resource_type="bank_account",
                resource_id=account.id,
                outcome=AuditOutcome.SUCCESS,
                actor_type=ActorType.USER,
                actor_user_id=actor_user_id,
                detail={"name": account.name},
            )
        )
        return account

    async def list_accounts(self, *, administration_id: uuid.UUID) -> Sequence[BankAccount]:
        return await self._repository.list_accounts(administration_id=administration_id)

    async def _account_or_refuse(
        self, *, administration_id: uuid.UUID, bank_account_id: uuid.UUID
    ) -> BankAccount:
        account = await self._repository.get_account(
            administration_id=administration_id, bank_account_id=bank_account_id
        )
        if account is None:
            raise BankAccountNotFound(f"bank account {bank_account_id} does not exist")
        return account

    # -- import -----------------------------------------------------------------

    async def import_statement(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        bank_account_id: uuid.UUID,
        csv_text: str,
        filename: str | None,
        actor_user_id: uuid.UUID,
    ) -> ImportResult:
        await self._account_or_refuse(
            administration_id=administration_id, bank_account_id=bank_account_id
        )
        rows = parse_statement_csv(csv_text)  # raises CsvStatementError, left to the caller

        import_id = await self._repository.create_import(
            organization_id=organization_id,
            administration_id=administration_id,
            bank_account_id=bank_account_id,
            filename=filename,
            user_id=actor_user_id,
        )

        inserted = 0
        duplicates = 0
        for row in rows:
            external_id = _external_id(bank_account_id, row)
            was_new = await self._repository.insert_transaction(
                organization_id=organization_id,
                administration_id=administration_id,
                bank_account_id=bank_account_id,
                import_id=import_id,
                row=row,
                external_id=external_id,
            )
            if was_new:
                inserted += 1
            else:
                duplicates += 1

        await self._repository.finalize_import(
            import_id=import_id, transaction_count=inserted, duplicate_count=duplicates
        )
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action="import_bank_statement",
                resource_type="bank_statement_import",
                resource_id=import_id,
                outcome=AuditOutcome.SUCCESS,
                actor_type=ActorType.USER,
                actor_user_id=actor_user_id,
                detail={
                    "bank_account_id": str(bank_account_id),
                    "transaction_count": inserted,
                    "duplicate_count": duplicates,
                },
            )
        )
        return ImportResult(
            import_id=import_id, transaction_count=inserted, duplicate_count=duplicates
        )

    # -- reads --------------------------------------------------------------

    async def list_transactions(
        self,
        *,
        administration_id: uuid.UUID,
        bank_account_id: uuid.UUID,
        status: TransactionStatus | None = None,
    ) -> Sequence[BankTransaction]:
        return await self._repository.list_transactions(
            administration_id=administration_id, bank_account_id=bank_account_id, status=status
        )

    async def get_transaction(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> BankTransaction:
        transaction = await self._repository.get_transaction(
            administration_id=administration_id, transaction_id=transaction_id
        )
        if transaction is None:
            raise TransactionNotFound(f"bank transaction {transaction_id} does not exist")
        return transaction

    async def match_candidates(
        self, *, administration_id: uuid.UUID, transaction: BankTransaction
    ) -> Sequence[MatchCandidate]:
        if not transaction.is_inflow:
            # A customer's payment is always money IN; an outflow has no
            # sales-invoice candidate by construction.
            return ()
        return await self._repository.match_candidates(
            administration_id=administration_id, amount=transaction.amount
        )

    # -- reconciliation -------------------------------------------------------

    async def _transaction_or_refuse(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> BankTransaction:
        transaction = await self._repository.get_transaction(
            administration_id=administration_id, transaction_id=transaction_id
        )
        if transaction is None:
            raise TransactionNotFound(f"bank transaction {transaction_id} does not exist")
        if transaction.status is TransactionStatus.RECONCILED:
            raise TransactionAlreadyReconciled(
                f"bank transaction {transaction_id} has already been reconciled"
            )
        return transaction

    async def reconcile_with_invoice(
        self,
        *,
        administration_id: uuid.UUID,
        transaction_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> BankTransaction:
        """FR-BNK's reconciliation, the invoice-match path: records a real
        payment through `SalesPaymentService`, which is where the balance
        check, the receivable control account and the period all already
        live - this method adds nothing to that except marking the bank
        transaction as the source.
        """
        transaction = await self._transaction_or_refuse(
            administration_id=administration_id, transaction_id=transaction_id
        )
        if not transaction.is_inflow:
            raise InvalidBankField("invoice_id", "an outflow cannot be matched to a sales invoice")
        bank_account = await self._account_or_refuse(
            administration_id=administration_id, bank_account_id=transaction.bank_account_id
        )

        try:
            payment = await self._payments.record(
                administration_id=administration_id,
                invoice_id=invoice_id,
                actor_user_id=actor_user_id,
                amount=transaction.amount,
                paid_on=transaction.booking_date,
                method=PaymentMethod.BANK_TRANSFER,
                bank_account_id=bank_account.ledger_account_id,
                reference=transaction.description,
            )
        except InvoicingError as exc:
            raise InvalidBankField("invoice_id", str(exc)) from exc

        reconciled = await self._repository.mark_reconciled(
            administration_id=administration_id,
            transaction_id=transaction_id,
            journal_entry_id=payment.journal_entry_id,
            matched_sales_invoice_id=invoice_id,
            matched_payment_id=payment.id,
            user_id=actor_user_id,
            reconciled_at=datetime.now().astimezone(),
        )
        await self._record_reconciled(
            administration_id, transaction_id, payment.journal_entry_id, actor_user_id
        )
        return reconciled

    async def reconcile_generic(
        self,
        *,
        administration_id: uuid.UUID,
        transaction_id: uuid.UUID,
        offset_account_id: uuid.UUID,
        description: str | None,
        actor_user_id: uuid.UUID,
    ) -> BankTransaction:
        """The fallback path: an expense paid, a bank fee, a transfer -
        anything that is not a customer settling an invoice. Posts a plain
        two-line entry against whichever account the caller names, into the
        administration's single active BANK journal (FR-GL-002).
        """
        transaction = await self._transaction_or_refuse(
            administration_id=administration_id, transaction_id=transaction_id
        )
        bank_account = await self._account_or_refuse(
            administration_id=administration_id, bank_account_id=transaction.bank_account_id
        )

        period_id = await self._repository.period_for_date(
            administration_id=administration_id, on=transaction.booking_date
        )
        if period_id is None:
            raise NoOpenPeriod(transaction.booking_date)
        journal_id = await self._repository.bank_journal_of(administration_id=administration_id)
        if journal_id is None:
            raise NoActiveBankJournal(
                "this administration has no single active bank journal to post into"
            )

        amount = abs(transaction.amount)
        entry_description = description or transaction.description or "Bank reconciliation"
        if transaction.is_inflow:
            lines = [
                LineInput(account_id=bank_account.ledger_account_id, debit=amount),
                LineInput(account_id=offset_account_id, credit=amount),
            ]
        else:
            lines = [
                LineInput(account_id=offset_account_id, debit=amount),
                LineInput(account_id=bank_account.ledger_account_id, credit=amount),
            ]

        posted = await self._ledger.post(
            EntryInput(
                administration_id=administration_id,
                journal_id=journal_id,
                period_id=period_id,
                entry_date=transaction.booking_date,
                description=entry_description,
                lines=lines,
            ),
            actor_user_id=actor_user_id,
        )

        reconciled = await self._repository.mark_reconciled(
            administration_id=administration_id,
            transaction_id=transaction_id,
            journal_entry_id=posted.id,
            matched_sales_invoice_id=None,
            matched_payment_id=None,
            user_id=actor_user_id,
            reconciled_at=datetime.now().astimezone(),
        )
        await self._record_reconciled(administration_id, transaction_id, posted.id, actor_user_id)
        return reconciled

    async def _record_reconciled(
        self,
        administration_id: uuid.UUID,
        transaction_id: uuid.UUID,
        journal_entry_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> None:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        assert organization_id is not None
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.POSTING,
                action="reconcile_bank_transaction",
                resource_type="bank_transaction",
                resource_id=transaction_id,
                outcome=AuditOutcome.SUCCESS,
                actor_type=ActorType.USER,
                actor_user_id=actor_user_id,
                detail={"journal_entry_id": str(journal_entry_id)},
            )
        )


def _external_id(bank_account_id: uuid.UUID, row: StatementRow) -> str:
    """A hash of the imported line's own content - stable across re-imports
    of the same file, so uploading it twice deduplicates rather than
    doubling the account's transactions (0065's own unique index is the
    actual guarantee; this is what makes two imports of one real statement
    produce the same value to check it against).
    """
    payload = "|".join(
        (
            str(bank_account_id),
            row.booking_date.isoformat(),
            str(row.amount),
            row.counterparty_iban or "",
            row.description or "",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
