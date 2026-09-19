"""SQL for invoice approval - migration 0060. Thin: the rules (what counts as approved, what a
stale approval is) live in `api.invoicing.approval`; the partial unique index is what holds
"one pending request per draft" under concurrency."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.invoicing.approval import InvoiceApproval, QueueEntry

_COLUMNS = """
    a.id, a.administration_id, a.invoice_id, a.content_hash, a.status,
    a.requested_by_user_id, a.requested_at, a.request_note,
    a.decided_by_user_id, a.decided_at, a.decision_reason
"""

_BARE = _COLUMNS.replace("a.", "")


def _approval(row: Any) -> InvoiceApproval:
    return InvoiceApproval(
        id=row.id,
        administration_id=row.administration_id,
        invoice_id=row.invoice_id,
        content_hash=row.content_hash,
        status=row.status,
        requested_by_user_id=row.requested_by_user_id,
        requested_at=row.requested_at,
        request_note=row.request_note,
        decided_by_user_id=row.decided_by_user_id,
        decided_at=row.decided_at,
        decision_reason=row.decision_reason,
    )


class SqlApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        async with self._session.begin_nested():
            yield

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    async def approval_required(self, *, administration_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            text("SELECT invoice_approval_required FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return bool(row.invoice_approval_required) if row is not None else False

    async def create_request(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        content_hash: str,
        user_id: uuid.UUID,
        note: str | None,
    ) -> InvoiceApproval:
        # An earlier request, decided or not, is replaced: its record stays (status
        # superseded), only its standing goes.
        await self._session.execute(
            text(
                "UPDATE sales_invoice_approval SET status = 'superseded' "
                " WHERE invoice_id = :invoice AND administration_id = :admin "
                "   AND status IN ('pending', 'approved')"
            ),
            {"invoice": str(invoice_id), "admin": str(administration_id)},
        )
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO sales_invoice_approval (
                    organization_id, administration_id, invoice_id, content_hash,
                    requested_by_user_id, request_note
                ) VALUES (
                    (SELECT organization_id FROM administration WHERE id = :admin),
                    :admin, :invoice, :hash, :user, :note
                )
                RETURNING {_BARE}
                """
            ),
            {
                "admin": str(administration_id),
                "invoice": str(invoice_id),
                "hash": content_hash,
                "user": str(user_id),
                "note": note,
            },
        )
        return _approval(result.one())

    async def pending_for_invoice(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> InvoiceApproval | None:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM sales_invoice_approval a "
                " WHERE a.invoice_id = :invoice AND a.administration_id = :admin "
                "   AND a.status = 'pending'"
            ),
            {"invoice": str(invoice_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _approval(row)

    async def latest_for_invoice(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> InvoiceApproval | None:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM sales_invoice_approval a "
                " WHERE a.invoice_id = :invoice AND a.administration_id = :admin "
                " ORDER BY a.requested_at DESC, a.id LIMIT 1"
            ),
            {"invoice": str(invoice_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _approval(row)

    async def decide(
        self,
        *,
        administration_id: uuid.UUID,
        approval_id: uuid.UUID,
        status: str,
        user_id: uuid.UUID,
        reason: str | None,
    ) -> InvoiceApproval | None:
        result = await self._session.execute(
            text(
                f"""
                UPDATE sales_invoice_approval
                   SET status = :status, decided_by_user_id = :user, decided_at = now(),
                       decision_reason = :reason
                 WHERE id = :id AND administration_id = :admin AND status = 'pending'
                RETURNING {_BARE}
                """
            ),
            {
                "status": status,
                "user": str(user_id),
                "reason": reason,
                "id": str(approval_id),
                "admin": str(administration_id),
            },
        )
        row = result.first()
        return None if row is None else _approval(row)

    async def queue(
        self, *, administration_id: uuid.UUID, status: str, limit: int
    ) -> Sequence[QueueEntry]:
        result = await self._session.execute(
            text(
                f"""
                SELECT {_COLUMNS}, si.customer_name, si.invoice_date, si.invoice_reference
                  FROM sales_invoice_approval a
                  JOIN sales_invoice si ON si.id = a.invoice_id
                 WHERE a.administration_id = :admin AND a.status = :status
                 ORDER BY a.requested_at
                 LIMIT :limit
                """
            ),
            {"admin": str(administration_id), "status": status, "limit": limit},
        )
        return [
            QueueEntry(
                approval=_approval(row),
                customer_name=row.customer_name,
                invoice_date=row.invoice_date,
                invoice_reference=row.invoice_reference,
            )
            for row in result
        ]
