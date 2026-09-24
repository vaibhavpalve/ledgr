"""SQL for bank accounts, statement imports and reconciliation - migration
0065.

A plain tenant-scoped module, not the ledger's bounded context: writes here
touch `bank_account`/`bank_transaction`/`bank_statement_import`, never
`journal_entry`/`journal_line` directly. The posting itself happens in
`api.bank.service` via `LedgerService.post()` or `SalesPaymentService.record()`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.bank.csv_parser import StatementRow
from api.bank.model import (
    BankAccount,
    BankAccountStatus,
    BankTransaction,
    MatchCandidate,
    TransactionStatus,
)

_ACCOUNT_COLUMNS = (
    "id, administration_id, name, iban, currency, ledger_account_id, status, created_at"
)

_TRANSACTION_COLUMNS = """
    id, administration_id, bank_account_id, booking_date, value_date, amount, currency,
    counterparty_name, counterparty_iban, description, external_id, status,
    matched_sales_invoice_id, matched_payment_id, journal_entry_id, reconciled_at
"""


def _account(row: Any) -> BankAccount:
    return BankAccount(
        id=row.id,
        administration_id=row.administration_id,
        name=row.name,
        iban=row.iban,
        currency=row.currency,
        ledger_account_id=row.ledger_account_id,
        status=BankAccountStatus(row.status),
        created_at=row.created_at,
    )


def _transaction(row: Any) -> BankTransaction:
    return BankTransaction(
        id=row.id,
        administration_id=row.administration_id,
        bank_account_id=row.bank_account_id,
        booking_date=row.booking_date,
        value_date=row.value_date,
        amount=Decimal(row.amount),
        currency=row.currency,
        counterparty_name=row.counterparty_name,
        counterparty_iban=row.counterparty_iban,
        description=row.description,
        external_id=row.external_id,
        status=TransactionStatus(row.status),
        matched_sales_invoice_id=row.matched_sales_invoice_id,
        matched_payment_id=row.matched_payment_id,
        journal_entry_id=row.journal_entry_id,
        reconciled_at=row.reconciled_at,
    )


class SqlBankRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- accounts -----------------------------------------------------------

    async def create_account(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        name: str,
        iban: str | None,
        currency: str,
        ledger_account_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> BankAccount:
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO bank_account (
                    organization_id, administration_id, name, iban, currency,
                    ledger_account_id, created_by_user_id
                ) VALUES (:org, :admin, :name, :iban, :currency, :ledger_account_id, :user)
                RETURNING {_ACCOUNT_COLUMNS}
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "name": name,
                "iban": iban,
                "currency": currency,
                "ledger_account_id": str(ledger_account_id),
                "user": str(user_id),
            },
        )
        return _account(result.one())

    async def get_account(
        self, *, administration_id: uuid.UUID, bank_account_id: uuid.UUID
    ) -> BankAccount | None:
        result = await self._session.execute(
            text(
                f"SELECT {_ACCOUNT_COLUMNS} FROM bank_account "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(bank_account_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _account(row)

    async def list_accounts(self, *, administration_id: uuid.UUID) -> Sequence[BankAccount]:
        result = await self._session.execute(
            text(
                f"SELECT {_ACCOUNT_COLUMNS} FROM bank_account "
                "WHERE administration_id = :admin AND status = 'active' ORDER BY name"
            ),
            {"admin": str(administration_id)},
        )
        return [_account(row) for row in result]

    async def account_type_of(
        self, *, administration_id: uuid.UUID, ledger_account_id: uuid.UUID
    ) -> str | None:
        result = await self._session.execute(
            text(
                "SELECT account_type FROM ledger_account "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(ledger_account_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.account_type

    # -- import ---------------------------------------------------------------

    async def create_import(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        bank_account_id: uuid.UUID,
        filename: str | None,
        user_id: uuid.UUID,
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "INSERT INTO bank_statement_import "
                "  (organization_id, administration_id, bank_account_id, source_format, "
                "   filename, imported_by_user_id) "
                "VALUES (:org, :admin, :account, 'csv', :filename, :user) "
                "RETURNING id"
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "account": str(bank_account_id),
                "filename": filename,
                "user": str(user_id),
            },
        )
        return result.scalar_one()  # type: ignore[no-any-return]

    async def insert_transaction(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        bank_account_id: uuid.UUID,
        import_id: uuid.UUID,
        row: StatementRow,
        external_id: str,
    ) -> bool:
        """Inserts one imported line; returns False (no-op) when its
        `external_id` was already imported for this bank account -
        `bank_transaction_dedup_idx` is what makes that safe under
        concurrent imports, this is just the ORM-level shape of it.
        """
        result = await self._session.execute(
            text(
                """
                INSERT INTO bank_transaction (
                    organization_id, administration_id, bank_account_id, import_id,
                    booking_date, amount, counterparty_name, counterparty_iban,
                    description, external_id
                ) VALUES (
                    :org, :admin, :account, :import_id,
                    :booking_date, :amount, :counterparty_name, :counterparty_iban,
                    :description, :external_id
                )
                ON CONFLICT (bank_account_id, external_id) DO NOTHING
                RETURNING id
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "account": str(bank_account_id),
                "import_id": str(import_id),
                "booking_date": row.booking_date,
                "amount": row.amount,
                "counterparty_name": row.counterparty_name,
                "counterparty_iban": row.counterparty_iban,
                "description": row.description,
                "external_id": external_id,
            },
        )
        return result.first() is not None

    async def finalize_import(
        self, *, import_id: uuid.UUID, transaction_count: int, duplicate_count: int
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE bank_statement_import "
                "SET transaction_count = :count, duplicate_count = :duplicates "
                "WHERE id = :id"
            ),
            {"id": str(import_id), "count": transaction_count, "duplicates": duplicate_count},
        )

    # -- transactions ---------------------------------------------------------

    async def get_transaction(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> BankTransaction | None:
        result = await self._session.execute(
            text(
                f"SELECT {_TRANSACTION_COLUMNS} FROM bank_transaction "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(transaction_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _transaction(row)

    async def list_transactions(
        self,
        *,
        administration_id: uuid.UUID,
        bank_account_id: uuid.UUID,
        status: TransactionStatus | None,
    ) -> Sequence[BankTransaction]:
        result = await self._session.execute(
            text(
                f"""
                SELECT {_TRANSACTION_COLUMNS} FROM bank_transaction
                 WHERE administration_id = :admin AND bank_account_id = :account
                   AND (cast(:status as text) IS NULL OR status = :status)
                 ORDER BY booking_date DESC, id DESC
                """
            ),
            {
                "admin": str(administration_id),
                "account": str(bank_account_id),
                "status": status.value if status else None,
            },
        )
        return [_transaction(row) for row in result]

    async def match_candidates(
        self, *, administration_id: uuid.UUID, amount: Decimal
    ) -> Sequence[MatchCandidate]:
        """Open sales invoices whose OUTSTANDING balance equals `amount`
        exactly - `invoicing.invoice_balances` (0052) is the one definition
        of "outstanding" every other screen already reads, reused rather
        than reimplemented here.
        """
        result = await self._session.execute(
            text(
                "SELECT invoice_id, invoice_reference, customer_name, outstanding, invoice_date "
                "FROM invoicing.invoice_balances(:admin, NULL) "
                "WHERE outstanding = :amount ORDER BY invoice_date"
            ),
            {"admin": str(administration_id), "amount": amount},
        )
        return [
            MatchCandidate(
                invoice_id=row.invoice_id,
                invoice_reference=row.invoice_reference,
                customer_name=row.customer_name,
                outstanding=Decimal(row.outstanding),
                invoice_date=row.invoice_date,
            )
            for row in result
        ]

    async def open_invoice_balances(self, *, administration_id: uuid.UUID) -> list[MatchCandidate]:
        """Every sales invoice with something outstanding - read once for a whole page of bank
        lines (ADR-091), rather than one query per line."""
        result = await self._session.execute(
            text(
                "SELECT invoice_id, invoice_reference, customer_name, outstanding, invoice_date "
                "FROM invoicing.invoice_balances(:admin, NULL) "
                "WHERE outstanding > 0 ORDER BY invoice_date"
            ),
            {"admin": str(administration_id)},
        )
        return [
            MatchCandidate(
                invoice_id=row.invoice_id,
                invoice_reference=row.invoice_reference,
                customer_name=row.customer_name,
                outstanding=Decimal(row.outstanding),
                invoice_date=row.invoice_date,
            )
            for row in result
        ]

    async def period_for_date(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        result = await self._session.execute(
            text(
                "SELECT id FROM period WHERE administration_id = :admin "
                "  AND :on BETWEEN start_date AND end_date AND status = 'open'"
            ),
            {"admin": str(administration_id), "on": on},
        )
        row = result.first()
        return None if row is None else row.id

    async def bank_journal_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text(
                "SELECT id FROM ledger_journal WHERE administration_id = :admin "
                "  AND journal_type = 'bank' AND status = 'active'"
            ),
            {"admin": str(administration_id)},
        )
        rows = list(result)
        return rows[0].id if len(rows) == 1 else None

    async def mark_reconciled(
        self,
        *,
        administration_id: uuid.UUID,
        transaction_id: uuid.UUID,
        journal_entry_id: uuid.UUID,
        matched_sales_invoice_id: uuid.UUID | None,
        matched_payment_id: uuid.UUID | None,
        user_id: uuid.UUID,
        reconciled_at: datetime,
    ) -> BankTransaction:
        result = await self._session.execute(
            text(
                f"""
                UPDATE bank_transaction SET
                    status = 'reconciled',
                    journal_entry_id = :entry,
                    matched_sales_invoice_id = :invoice,
                    matched_payment_id = :payment,
                    reconciled_by_user_id = :user,
                    reconciled_at = :reconciled_at
                 WHERE id = :id AND administration_id = :admin
                RETURNING {_TRANSACTION_COLUMNS}
                """
            ),
            {
                "id": str(transaction_id),
                "admin": str(administration_id),
                "entry": str(journal_entry_id),
                "invoice": str(matched_sales_invoice_id) if matched_sales_invoice_id else None,
                "payment": str(matched_payment_id) if matched_payment_id else None,
                "user": str(user_id),
                "reconciled_at": reconciled_at,
            },
        )
        return _transaction(result.one())

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id
