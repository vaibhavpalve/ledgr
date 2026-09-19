"""SQL for sales-invoice payments - migration 0052.

Thin on purpose, like `api.invoicing.repository`. Nothing here touches
`journal_entry` or `journal_line`: the receipt is written by `LedgerService`, and
`ledgr_app` holds no grant that would let this file do otherwise (0020).

"Outstanding" is defined once, in `invoicing.invoice_balances` (0052), and read
from there - never recomputed in this file.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from api.invoicing.payments import (
    InvoiceBalance,
    InvoicePayment,
    PayableInvoice,
    PaymentExceedsOutstanding,
    PaymentMethod,
)
from api.invoicing.posting_repository import SqlSalesPostingRepository

_PAYMENT_COLUMNS = """
    id, administration_id, invoice_id, amount, paid_on, method, reference,
    bank_account_id, journal_entry_id, recorded_by_user_id, recorded_at,
    voided_at, void_journal_entry_id
"""


def _payment(row: Any) -> InvoicePayment:
    return InvoicePayment(
        id=row.id,
        administration_id=row.administration_id,
        invoice_id=row.invoice_id,
        amount=Decimal(row.amount),
        paid_on=row.paid_on,
        method=PaymentMethod(row.method),
        reference=row.reference,
        bank_account_id=row.bank_account_id,
        journal_entry_id=row.journal_entry_id,
        recorded_by_user_id=row.recorded_by_user_id,
        recorded_at=row.recorded_at,
        voided_at=row.voided_at,
        void_journal_entry_id=row.void_journal_entry_id,
    )


class SqlPaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        # The period and control-account lookups are exactly the posting side's;
        # one implementation of "the open period containing a date" rather than
        # two that could come to disagree.
        self._posting = SqlSalesPostingRepository(session)

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    async def invoice(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> PayableInvoice | None:
        result = await self._session.execute(
            text(
                "SELECT id, administration_id, status, credits_invoice_id, "
                "       invoice_reference, customer_name "
                "  FROM sales_invoice WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(invoice_id), "admin": str(administration_id)},
        )
        row = result.first()
        if row is None:
            return None
        return PayableInvoice(
            id=row.id,
            administration_id=row.administration_id,
            is_issued=row.status == "issued",
            is_credit_note=row.credits_invoice_id is not None,
            invoice_reference=row.invoice_reference,
            customer_name=row.customer_name,
        )

    async def balance_of(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> InvoiceBalance | None:
        result = await self._session.execute(
            text(
                "SELECT invoice_id, gross, credited, paid "
                "  FROM invoicing.invoice_balances(:admin, :invoice)"
            ),
            {"admin": str(administration_id), "invoice": str(invoice_id)},
        )
        row = result.first()
        if row is None:
            return None
        return InvoiceBalance(
            invoice_id=row.invoice_id,
            gross=Decimal(row.gross),
            credited=Decimal(row.credited),
            paid=Decimal(row.paid),
        )

    async def journal_of_type(
        self, *, administration_id: uuid.UUID, journal_type: str
    ) -> uuid.UUID | None:
        result = await self._session.execute(
            text(
                "SELECT id FROM ledger_journal "
                " WHERE administration_id = :admin "
                "   AND journal_type = :type AND status = 'active'"
            ),
            {"admin": str(administration_id), "type": journal_type},
        )
        rows = result.fetchall()
        return rows[0].id if len(rows) == 1 else None

    async def open_period_for(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        return await self._posting.open_period_for(administration_id=administration_id, on=on)

    async def receivable_control_account(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return await self._posting.receivable_control_account(administration_id=administration_id)

    async def invoice_party(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> uuid.UUID | None:
        """The debtor the invoice was posted against, by the two ways
        `SalesPostingService._party_for` creates one: the mastered customer's
        party, else the one-off party named after the invoice."""
        mastered = await self._session.execute(
            text(
                "SELECT c.subledger_party_id AS party_id "
                "  FROM sales_invoice si "
                "  JOIN customer c ON c.id = si.customer_id "
                " WHERE si.id = :invoice AND si.administration_id = :admin"
            ),
            {"invoice": str(invoice_id), "admin": str(administration_id)},
        )
        row = mastered.first()
        if row is not None and row.party_id is not None:
            return row.party_id  # type: ignore[no-any-return]

        one_off = await self._session.execute(
            text(
                "SELECT id FROM subledger_party "
                " WHERE administration_id = :admin AND external_reference = :ref"
            ),
            {"admin": str(administration_id), "ref": f"sales_invoice:{invoice_id}"},
        )
        found = one_off.first()
        return None if found is None else found.id

    async def insert_payment(self, payment: InvoicePayment) -> InvoicePayment:
        try:
            result = await self._session.execute(
                text(
                    f"""
                    INSERT INTO sales_invoice_payment (
                        id, organization_id, administration_id, invoice_id, amount,
                        paid_on, method, reference, bank_account_id, journal_entry_id,
                        recorded_by_user_id
                    ) VALUES (
                        :id,
                        -- overwritten by sales_invoice_payment_guard(); the
                        -- placeholder exists because the column is NOT NULL.
                        (SELECT organization_id FROM sales_invoice WHERE id = :invoice),
                        :admin, :invoice, :amount, :paid_on, :method, :reference,
                        :bank, :entry, :user
                    )
                    RETURNING {_PAYMENT_COLUMNS}
                    """
                ),
                {
                    "id": str(payment.id),
                    "admin": str(payment.administration_id),
                    "invoice": str(payment.invoice_id),
                    "amount": payment.amount,
                    "paid_on": payment.paid_on,
                    "method": payment.method.value,
                    "reference": payment.reference,
                    "bank": str(payment.bank_account_id),
                    "entry": str(payment.journal_entry_id),
                    "user": str(payment.recorded_by_user_id),
                },
            )
        except DBAPIError as exc:
            # Two payments recorded at the same moment can both pass the service's
            # check; the trigger's row lock is what actually refuses the second.
            # Translated so that race answers like the ordinary case, not as a 500.
            if "still outstanding" in str(exc.orig):
                raise PaymentExceedsOutstanding(Decimal(0)) from exc
            raise
        return _payment(result.one())

    async def get_payment(
        self, *, administration_id: uuid.UUID, payment_id: uuid.UUID
    ) -> InvoicePayment | None:
        result = await self._session.execute(
            text(
                f"SELECT {_PAYMENT_COLUMNS} FROM sales_invoice_payment "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(payment_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _payment(row)

    async def payments_for(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Sequence[InvoicePayment]:
        result = await self._session.execute(
            text(
                f"SELECT {_PAYMENT_COLUMNS} FROM sales_invoice_payment "
                "WHERE invoice_id = :invoice AND administration_id = :admin "
                "ORDER BY paid_on, recorded_at"
            ),
            {"invoice": str(invoice_id), "admin": str(administration_id)},
        )
        return [_payment(row) for row in result]

    async def mark_voided(
        self,
        *,
        administration_id: uuid.UUID,
        payment_id: uuid.UUID,
        user_id: uuid.UUID,
        void_journal_entry_id: uuid.UUID,
    ) -> InvoicePayment:
        result = await self._session.execute(
            text(
                f"""
                UPDATE sales_invoice_payment
                   SET voided_at = now(), voided_by_user_id = :user,
                       void_journal_entry_id = :entry
                 WHERE id = :id AND administration_id = :admin AND voided_at IS NULL
                RETURNING {_PAYMENT_COLUMNS}
                """
            ),
            {
                "user": str(user_id),
                "entry": str(void_journal_entry_id),
                "id": str(payment_id),
                "admin": str(administration_id),
            },
        )
        return _payment(result.one())
