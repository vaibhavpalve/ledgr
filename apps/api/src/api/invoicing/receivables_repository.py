"""SQL for aged receivables and statements - migration 0054.

Thin: the two functions decide which items and movements exist as of a date, and
`api.invoicing.receivables` does the arithmetic. Nothing here computes a balance.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.invoicing.receivables import MovementKind, OpenItem, StatementMovement


class SqlReceivablesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    async def open_items(self, *, administration_id: uuid.UUID, as_of: date) -> Sequence[OpenItem]:
        result = await self._session.execute(
            text(
                "SELECT invoice_id, invoice_reference, invoice_date, due_date, customer_id, "
                "       customer_name, outstanding "
                "  FROM invoicing.receivable_items(:admin, :as_of)"
            ),
            {"admin": str(administration_id), "as_of": as_of},
        )
        return [
            OpenItem(
                invoice_id=row.invoice_id,
                invoice_reference=row.invoice_reference,
                invoice_date=row.invoice_date,
                due_date=row.due_date,
                customer_id=row.customer_id,
                customer_name=row.customer_name,
                outstanding=Decimal(row.outstanding),
            )
            for row in result
        ]

    async def customer_name(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> str | None:
        result = await self._session.execute(
            text("SELECT name FROM customer WHERE id = :id AND administration_id = :admin"),
            {"id": str(customer_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.name

    async def movements(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> Sequence[StatementMovement]:
        result = await self._session.execute(
            text(
                "SELECT movement_date, kind, reference, invoice_id, debit, credit "
                "  FROM invoicing.customer_movements(:admin, :customer)"
            ),
            {"admin": str(administration_id), "customer": str(customer_id)},
        )
        return [
            StatementMovement(
                movement_date=row.movement_date,
                kind=MovementKind(row.kind),
                reference=row.reference,
                invoice_id=row.invoice_id,
                debit=Decimal(row.debit),
                credit=Decimal(row.credit),
            )
            for row in result
        ]
