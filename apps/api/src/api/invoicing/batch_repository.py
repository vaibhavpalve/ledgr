"""SQL for batch invoicing - migration 0061. Written once; nothing here updates or deletes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.invoicing.batch_service import BatchItem, BatchKeyTaken, BatchRecord

_BATCH = """
    id, administration_id, name, batch_key, invoice_date, issue_requested, item_count,
    drafted_count, issued_count, failed_count, created_by_user_id, created_at
"""
_ITEM = "id, batch_id, position, customer_id, status, invoice_id, error_code, issue_error"


def _record(row: Any) -> BatchRecord:
    return BatchRecord(
        id=row.id,
        administration_id=row.administration_id,
        name=row.name,
        batch_key=row.batch_key,
        invoice_date=row.invoice_date,
        issue_requested=row.issue_requested,
        item_count=row.item_count,
        drafted_count=row.drafted_count,
        issued_count=row.issued_count,
        failed_count=row.failed_count,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
    )


def _item(row: Any) -> BatchItem:
    return BatchItem(
        id=row.id,
        batch_id=row.batch_id,
        position=row.position,
        customer_id=row.customer_id,
        status=row.status,
        invoice_id=row.invoice_id,
        error_code=row.error_code,
        issue_error=row.issue_error,
    )


class SqlBatchRepository:
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

    async def batch_by_key(
        self, *, administration_id: uuid.UUID, batch_key: str
    ) -> BatchRecord | None:
        result = await self._session.execute(
            text(
                f"SELECT {_BATCH} FROM sales_invoice_batch "
                "WHERE administration_id = :admin AND batch_key = :key"
            ),
            {"admin": str(administration_id), "key": batch_key},
        )
        row = result.first()
        return None if row is None else _record(row)

    async def insert_batch(self, batch: BatchRecord, items: Sequence[BatchItem]) -> None:
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    text(
                        """
                        INSERT INTO sales_invoice_batch (
                            id, organization_id, administration_id, name, batch_key,
                            invoice_date, issue_requested, item_count, drafted_count,
                            issued_count, failed_count, created_by_user_id
                        ) VALUES (
                            :id, (SELECT organization_id FROM administration WHERE id = :admin),
                            :admin, :name, :key, :on, :issue, :items, :drafted, :issued,
                            :failed, :user
                        )
                        """
                    ),
                    {
                        "id": str(batch.id),
                        "admin": str(batch.administration_id),
                        "name": batch.name,
                        "key": batch.batch_key,
                        "on": batch.invoice_date,
                        "issue": batch.issue_requested,
                        "items": batch.item_count,
                        "drafted": batch.drafted_count,
                        "issued": batch.issued_count,
                        "failed": batch.failed_count,
                        "user": str(batch.created_by_user_id),
                    },
                )
        except IntegrityError as exc:
            if "sales_invoice_batch_key_idx" in str(exc.orig):
                raise BatchKeyTaken(batch.batch_key or "") from exc
            raise
        for item in items:
            await self._session.execute(
                text(
                    """
                    INSERT INTO sales_invoice_batch_item (
                        id, organization_id, administration_id, batch_id, position,
                        customer_id, invoice_id, status, error_code, issue_error
                    ) VALUES (
                        :id, (SELECT organization_id FROM administration WHERE id = :admin),
                        :admin, :batch, :position, :customer, :invoice, :status, :error, :issue
                    )
                    """
                ),
                {
                    "id": str(item.id),
                    "admin": str(batch.administration_id),
                    "batch": str(batch.id),
                    "position": item.position,
                    "customer": str(item.customer_id),
                    "invoice": None if item.invoice_id is None else str(item.invoice_id),
                    "status": item.status,
                    "error": item.error_code,
                    "issue": item.issue_error,
                },
            )

    async def get_batch(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID
    ) -> BatchRecord | None:
        result = await self._session.execute(
            text(
                f"SELECT {_BATCH} FROM sales_invoice_batch "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(batch_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _record(row)

    async def items_for_batch(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID
    ) -> Sequence[BatchItem]:
        result = await self._session.execute(
            text(
                f"SELECT {_ITEM} FROM sales_invoice_batch_item "
                "WHERE batch_id = :batch AND administration_id = :admin ORDER BY position"
            ),
            {"batch": str(batch_id), "admin": str(administration_id)},
        )
        return [_item(row) for row in result]

    async def list_batches(
        self, *, administration_id: uuid.UUID, limit: int
    ) -> Sequence[BatchRecord]:
        result = await self._session.execute(
            text(
                f"SELECT {_BATCH} FROM sales_invoice_batch "
                "WHERE administration_id = :admin ORDER BY created_at DESC LIMIT :limit"
            ),
            {"admin": str(administration_id), "limit": limit},
        )
        return [_record(row) for row in result]
