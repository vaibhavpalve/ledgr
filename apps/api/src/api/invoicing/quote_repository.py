"""SQL for quotes - migration 0057.

Thin, like `api.invoicing.repository`. The lifecycle rules live in
`api.invoicing.quotes` (and are restated by 0057's triggers); this file stores what it
is told. `transition` and `mark_converted` are CONDITIONAL updates, so a quote that
somebody else moved first is reported rather than overwritten.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.invoicing.quote_service import Quote, QuoteDraft
from api.invoicing.quotes import QuoteKind, QuoteLine, QuoteStatus

_COLUMNS = """
    id, administration_id, kind, quote_number, reference, customer_id, subject, valid_until,
    notes, status, sent_at, accepted_at, accepted_by_name, acceptance_reference, declined_at,
    decline_reason, cancelled_at, converted_at, converted_invoice_id
"""

#: The timestamp column each status stamps, so `transition` sets it in the same
#: statement that moves the status.
_STAMP = {
    QuoteStatus.SENT: "sent_at",
    QuoteStatus.ACCEPTED: "accepted_at",
    QuoteStatus.DECLINED: "declined_at",
    QuoteStatus.CANCELLED: "cancelled_at",
}


def _line(row: Any) -> QuoteLine:
    return QuoteLine(
        description=row.description,
        quantity=Decimal(row.quantity),
        unit_price=Decimal(row.unit_price),
        vat_treatment=row.vat_treatment,
        discount_percent=Decimal(row.discount_percent),
    )


def _quote(row: Any, lines: Sequence[QuoteLine]) -> Quote:
    return Quote(
        id=row.id,
        administration_id=row.administration_id,
        kind=QuoteKind(row.kind),
        quote_number=row.quote_number,
        reference=row.reference,
        customer_id=row.customer_id,
        subject=row.subject,
        valid_until=row.valid_until,
        notes=row.notes,
        status=QuoteStatus(row.status),
        lines=tuple(lines),
        sent_at=row.sent_at,
        accepted_at=row.accepted_at,
        accepted_by_name=row.accepted_by_name,
        acceptance_reference=row.acceptance_reference,
        declined_at=row.declined_at,
        decline_reason=row.decline_reason,
        cancelled_at=row.cancelled_at,
        converted_at=row.converted_at,
        converted_invoice_id=row.converted_invoice_id,
    )


class SqlQuoteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        """A SAVEPOINT. A conversion's draft invoice and its `converted` mark are
        written in one, so a race that loses takes its own draft with it."""
        async with self._session.begin_nested():
            yield

    async def customer_exists(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text("SELECT 1 FROM customer WHERE id = :id AND administration_id = :admin"),
            {"id": str(customer_id), "admin": str(administration_id)},
        )
        return result.first() is not None

    # -- definitions ------------------------------------------------------------------

    async def create(
        self, *, administration_id: uuid.UUID, user_id: uuid.UUID, draft: QuoteDraft
    ) -> Quote:
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO sales_quote (
                    organization_id, administration_id, kind, quote_number, reference,
                    customer_id, subject, valid_until, notes, created_by_user_id
                ) VALUES (
                    -- organization_id, quote_number and reference are all set by
                    -- sales_quote_before_insert(); the placeholders satisfy NOT NULL.
                    (SELECT organization_id FROM administration WHERE id = :admin),
                    :admin, :kind, 0, '', :customer, :subject, :valid_until, :notes, :user
                )
                RETURNING {_COLUMNS}
                """
            ),
            {
                "admin": str(administration_id),
                "kind": draft.kind.value,
                "customer": str(draft.customer_id),
                "subject": draft.subject,
                "valid_until": draft.valid_until,
                "notes": draft.notes,
                "user": str(user_id),
            },
        )
        row = result.one()
        await self._insert_lines(administration_id, row.id, draft.lines)
        return await self._reread(administration_id, row.id)

    async def replace(
        self, *, administration_id: uuid.UUID, quote_id: uuid.UUID, draft: QuoteDraft
    ) -> Quote:
        await self._session.execute(
            text(
                "UPDATE sales_quote SET customer_id = :customer, subject = :subject, "
                "  valid_until = :valid_until, notes = :notes "
                " WHERE id = :id AND administration_id = :admin"
            ),
            {
                "customer": str(draft.customer_id),
                "subject": draft.subject,
                "valid_until": draft.valid_until,
                "notes": draft.notes,
                "id": str(quote_id),
                "admin": str(administration_id),
            },
        )
        await self._session.execute(
            text("DELETE FROM sales_quote_line WHERE quote_id = :id"), {"id": str(quote_id)}
        )
        await self._insert_lines(administration_id, quote_id, draft.lines)
        return await self._reread(administration_id, quote_id)

    async def _insert_lines(
        self, administration_id: uuid.UUID, quote_id: uuid.UUID, lines: Sequence[QuoteLine]
    ) -> None:
        for position, line in enumerate(lines, start=1):
            await self._session.execute(
                text(
                    """
                    INSERT INTO sales_quote_line (
                        organization_id, administration_id, quote_id, position, description,
                        quantity, unit_price, discount_percent, vat_treatment
                    ) VALUES (
                        -- overwritten by sales_quote_line_guard()
                        (SELECT organization_id FROM sales_quote WHERE id = :quote),
                        :admin, :quote, :position, :description, :quantity, :unit_price,
                        :discount, :treatment
                    )
                    """
                ),
                {
                    "admin": str(administration_id),
                    "quote": str(quote_id),
                    "position": position,
                    "description": line.description,
                    "quantity": line.quantity,
                    "unit_price": line.unit_price,
                    "discount": line.discount_percent,
                    "treatment": line.vat_treatment,
                },
            )

    async def _reread(self, administration_id: uuid.UUID, quote_id: uuid.UUID) -> Quote:
        """What was WRITTEN, as the database stores it - so create, replace and a later
        read answer identically (`49.95` here and `49.9500` there is a difference a
        client would rightly report)."""
        found = await self.get(administration_id=administration_id, quote_id=quote_id)
        assert found is not None  # just written in this transaction
        return found

    # -- reading ------------------------------------------------------------------------

    async def _with_lines(self, rows: Sequence[Any]) -> list[Quote]:
        if not rows:
            return []
        by_quote: dict[uuid.UUID, list[QuoteLine]] = {}
        result = await self._session.execute(
            text(
                "SELECT quote_id, description, quantity, unit_price, discount_percent, "
                "       vat_treatment FROM sales_quote_line "
                " WHERE quote_id = ANY(CAST(:ids AS uuid[])) ORDER BY quote_id, position"
            ),
            {"ids": [str(row.id) for row in rows]},
        )
        for line in result:
            by_quote.setdefault(line.quote_id, []).append(_line(line))
        return [_quote(row, by_quote.get(row.id, ())) for row in rows]

    async def get(self, *, administration_id: uuid.UUID, quote_id: uuid.UUID) -> Quote | None:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM sales_quote WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(quote_id), "admin": str(administration_id)},
        )
        found = await self._with_lines(result.all())
        return found[0] if found else None

    async def list(
        self, *, administration_id: uuid.UUID, status: QuoteStatus | None
    ) -> Sequence[Quote]:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM sales_quote WHERE administration_id = :admin "
                "  AND (CAST(:status AS text) IS NULL OR status = :status) "
                "ORDER BY created_at DESC, quote_number DESC"
            ),
            {"admin": str(administration_id), "status": status.value if status else None},
        )
        return await self._with_lines(result.all())

    # -- state ----------------------------------------------------------------------------

    async def transition(
        self,
        *,
        administration_id: uuid.UUID,
        quote_id: uuid.UUID,
        expected: frozenset[QuoteStatus],
        to: QuoteStatus,
        user_id: uuid.UUID,
        accepted_by_name: str | None = None,
        acceptance_reference: str | None = None,
        decline_reason: str | None = None,
    ) -> bool:
        stamp = _STAMP[to]
        accepted = to is QuoteStatus.ACCEPTED
        result = await self._session.execute(
            text(
                f"""
                UPDATE sales_quote SET status = :to, {stamp} = now(),
                       accepted_by_user_id = CASE WHEN :accepted THEN :user
                                                  ELSE accepted_by_user_id END,
                       accepted_by_name = CASE WHEN :accepted THEN :name
                                               ELSE accepted_by_name END,
                       acceptance_reference = CASE WHEN :accepted THEN :reference
                                                   ELSE acceptance_reference END,
                       decline_reason = CASE WHEN :to = 'declined' THEN :reason
                                             ELSE decline_reason END
                 WHERE id = :id AND administration_id = :admin
                   AND status = ANY(CAST(:expected AS text[]))
                RETURNING id
                """
            ),
            {
                "to": to.value,
                "accepted": accepted,
                "user": str(user_id),
                "name": accepted_by_name,
                "reference": acceptance_reference,
                "reason": decline_reason,
                "id": str(quote_id),
                "admin": str(administration_id),
                "expected": [status.value for status in expected],
            },
        )
        return result.first() is not None

    async def extend_validity(
        self, *, administration_id: uuid.UUID, quote_id: uuid.UUID, valid_until: date
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE sales_quote SET valid_until = :valid_until "
                " WHERE id = :id AND administration_id = :admin"
            ),
            {"valid_until": valid_until, "id": str(quote_id), "admin": str(administration_id)},
        )

    async def fiscal_year_for(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        result = await self._session.execute(
            text(
                "SELECT id FROM fiscal_year "
                " WHERE administration_id = :admin AND :on BETWEEN start_date AND end_date "
                " ORDER BY (status = 'open') DESC LIMIT 1"
            ),
            {"admin": str(administration_id), "on": on},
        )
        row = result.first()
        return None if row is None else row.id

    async def mark_converted(
        self, *, administration_id: uuid.UUID, quote_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text(
                "UPDATE sales_quote SET status = 'converted', converted_at = now(), "
                "       converted_invoice_id = :invoice "
                " WHERE id = :id AND administration_id = :admin AND status = 'accepted' "
                "RETURNING id"
            ),
            {"invoice": str(invoice_id), "id": str(quote_id), "admin": str(administration_id)},
        )
        return result.first() is not None
