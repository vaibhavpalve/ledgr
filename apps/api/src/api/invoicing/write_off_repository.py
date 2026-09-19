"""SQL for bad-debt write-offs - migration 0058.

Thin, like `payments_repository`. Nothing here touches `journal_entry` or `journal_line`:
both entries are written by `LedgerService`. "Outstanding" is read from
`invoicing.invoice_balances`, never recomputed here, and the lookups a write-off shares with a
payment (journal, period, control account, debtor) are delegated to `SqlPaymentRepository`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from api.invoicing.bad_debt import VatGroupTotal
from api.invoicing.payments import InvoiceBalance
from api.invoicing.payments_repository import SqlPaymentRepository
from api.invoicing.posting_repository import SqlSalesPostingRepository
from api.invoicing.write_off_service import (
    VAT_OUTPUT,
    InvoiceWriteOff,
    NothingOutstanding,
    VatSplitLine,
    WriteOffInvoice,
)

_COLUMNS = """
    id, administration_id, invoice_id, amount, vat_amount, vat_split, written_off_on,
    reason, customer_insolvent, expense_account_id, journal_entry_id,
    recorded_by_user_id, recorded_at, vat_reclaimed_on, vat_reclaim_journal_entry_id,
    voided_at, void_journal_entry_id, void_reclaim_journal_entry_id
"""


def _write_off(row: Any) -> InvoiceWriteOff:
    split = row.vat_split if isinstance(row.vat_split, list) else json.loads(row.vat_split)
    return InvoiceWriteOff(
        id=row.id,
        administration_id=row.administration_id,
        invoice_id=row.invoice_id,
        amount=Decimal(row.amount),
        vat_amount=Decimal(row.vat_amount),
        vat_split=tuple(VatSplitLine(item["treatment"], Decimal(item["vat"])) for item in split),
        written_off_on=row.written_off_on,
        reason=row.reason,
        customer_insolvent=row.customer_insolvent,
        expense_account_id=row.expense_account_id,
        journal_entry_id=row.journal_entry_id,
        recorded_by_user_id=row.recorded_by_user_id,
        recorded_at=row.recorded_at,
        vat_reclaimed_on=row.vat_reclaimed_on,
        vat_reclaim_journal_entry_id=row.vat_reclaim_journal_entry_id,
        voided_at=row.voided_at,
        void_journal_entry_id=row.void_journal_entry_id,
        void_reclaim_journal_entry_id=row.void_reclaim_journal_entry_id,
    )


class SqlWriteOffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._payments = SqlPaymentRepository(session)
        self._posting = SqlSalesPostingRepository(session)

    # -- shared with payments ---------------------------------------------------------

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return await self._payments.organization_of(administration_id=administration_id)

    async def balance_of(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> InvoiceBalance | None:
        return await self._payments.balance_of(
            administration_id=administration_id, invoice_id=invoice_id
        )

    async def journal_of_type(
        self, *, administration_id: uuid.UUID, journal_type: str
    ) -> uuid.UUID | None:
        return await self._payments.journal_of_type(
            administration_id=administration_id, journal_type=journal_type
        )

    async def open_period_for(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        return await self._payments.open_period_for(administration_id=administration_id, on=on)

    async def receivable_control_account(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return await self._payments.receivable_control_account(administration_id=administration_id)

    async def invoice_party(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> uuid.UUID | None:
        return await self._payments.invoice_party(
            administration_id=administration_id, invoice_id=invoice_id
        )

    # -- the invoice ----------------------------------------------------------------------

    async def invoice(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> WriteOffInvoice | None:
        result = await self._session.execute(
            text(
                "SELECT id, administration_id, status, credits_invoice_id, "
                "       invoice_reference, customer_name, invoice_date, due_date "
                "  FROM sales_invoice WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(invoice_id), "admin": str(administration_id)},
        )
        row = result.first()
        if row is None:
            return None
        return WriteOffInvoice(
            id=row.id,
            administration_id=row.administration_id,
            is_issued=row.status == "issued",
            is_credit_note=row.credits_invoice_id is not None,
            invoice_reference=row.invoice_reference,
            customer_name=row.customer_name,
            invoice_date=row.invoice_date,
            due_date=row.due_date,
        )

    async def vat_groups(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Sequence[VatGroupTotal]:
        result = await self._session.execute(
            text(
                "SELECT vat_treatment, taxable_amount, vat_amount "
                "  FROM sales_invoice_vat_total "
                " WHERE invoice_id = :invoice AND administration_id = :admin "
                " ORDER BY vat_treatment"
            ),
            {"invoice": str(invoice_id), "admin": str(administration_id)},
        )
        return [
            VatGroupTotal(
                treatment=row.vat_treatment,
                taxable=Decimal(row.taxable_amount),
                vat=Decimal(row.vat_amount),
            )
            for row in result
        ]

    async def is_expense_account(
        self, *, administration_id: uuid.UUID, account_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text(
                "SELECT 1 FROM ledger_account "
                " WHERE id = :id AND administration_id = :admin AND account_type = 'expense'"
            ),
            {"id": str(account_id), "admin": str(administration_id)},
        )
        return result.first() is not None

    async def vat_output_account(
        self, *, administration_id: uuid.UUID, vat_treatment: str
    ) -> uuid.UUID | None:
        return await self._posting.posting_account(
            administration_id=administration_id, purpose=VAT_OUTPUT, vat_treatment=vat_treatment
        )

    # -- the write-off ----------------------------------------------------------------------

    async def insert_write_off(self, write_off: InvoiceWriteOff) -> InvoiceWriteOff:
        split = json.dumps(
            [{"treatment": line.treatment, "vat": str(line.vat)} for line in write_off.vat_split]
        )
        try:
            result = await self._session.execute(
                text(
                    f"""
                    INSERT INTO sales_invoice_write_off (
                        id, organization_id, administration_id, invoice_id, amount,
                        vat_amount, vat_split, written_off_on, reason, customer_insolvent,
                        expense_account_id, journal_entry_id, recorded_by_user_id
                    ) VALUES (
                        :id,
                        -- overwritten by sales_invoice_write_off_guard(); the placeholder
                        -- exists because the column is NOT NULL.
                        (SELECT organization_id FROM sales_invoice WHERE id = :invoice),
                        :admin, :invoice, :amount, :vat_amount, CAST(:split AS jsonb),
                        :on, :reason, :insolvent, :expense, :entry, :user
                    )
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "id": str(write_off.id),
                    "admin": str(write_off.administration_id),
                    "invoice": str(write_off.invoice_id),
                    "amount": write_off.amount,
                    "vat_amount": write_off.vat_amount,
                    "split": split,
                    "on": write_off.written_off_on,
                    "reason": write_off.reason,
                    "insolvent": write_off.customer_insolvent,
                    "expense": str(write_off.expense_account_id),
                    "entry": str(write_off.journal_entry_id),
                    "user": str(write_off.recorded_by_user_id),
                },
            )
        except DBAPIError as exc:
            # A payment or another write-off recorded at the same moment can both pass the
            # service's check; the trigger's row lock is what actually refuses the second.
            if "still outstanding" in str(exc.orig):
                raise NothingOutstanding("the balance changed while writing it off") from exc
            raise
        return _write_off(result.one())

    async def get_write_off(
        self, *, administration_id: uuid.UUID, write_off_id: uuid.UUID
    ) -> InvoiceWriteOff | None:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM sales_invoice_write_off "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(write_off_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _write_off(row)

    async def write_offs_for(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Sequence[InvoiceWriteOff]:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM sales_invoice_write_off "
                "WHERE invoice_id = :invoice AND administration_id = :admin "
                "ORDER BY written_off_on, recorded_at"
            ),
            {"invoice": str(invoice_id), "admin": str(administration_id)},
        )
        return [_write_off(row) for row in result]

    async def mark_vat_reclaimed(
        self,
        *,
        administration_id: uuid.UUID,
        write_off_id: uuid.UUID,
        user_id: uuid.UUID,
        reclaimed_on: date,
        journal_entry_id: uuid.UUID,
    ) -> InvoiceWriteOff:
        result = await self._session.execute(
            text(
                f"""
                UPDATE sales_invoice_write_off
                   SET vat_reclaimed_on = :on, vat_reclaim_journal_entry_id = :entry,
                       vat_reclaimed_by_user_id = :user
                 WHERE id = :id AND administration_id = :admin
                   AND voided_at IS NULL AND vat_reclaim_journal_entry_id IS NULL
                RETURNING {_COLUMNS}
                """
            ),
            {
                "on": reclaimed_on,
                "entry": str(journal_entry_id),
                "user": str(user_id),
                "id": str(write_off_id),
                "admin": str(administration_id),
            },
        )
        return _write_off(result.one())

    async def mark_voided(
        self,
        *,
        administration_id: uuid.UUID,
        write_off_id: uuid.UUID,
        user_id: uuid.UUID,
        void_journal_entry_id: uuid.UUID,
        void_reclaim_journal_entry_id: uuid.UUID | None,
    ) -> InvoiceWriteOff:
        result = await self._session.execute(
            text(
                f"""
                UPDATE sales_invoice_write_off
                   SET voided_at = now(), voided_by_user_id = :user,
                       void_journal_entry_id = :entry,
                       void_reclaim_journal_entry_id = :reclaim_entry
                 WHERE id = :id AND administration_id = :admin AND voided_at IS NULL
                RETURNING {_COLUMNS}
                """
            ),
            {
                "user": str(user_id),
                "entry": str(void_journal_entry_id),
                "reclaim_entry": None
                if void_reclaim_journal_entry_id is None
                else str(void_reclaim_journal_entry_id),
                "id": str(write_off_id),
                "admin": str(administration_id),
            },
        )
        return _write_off(result.one())
