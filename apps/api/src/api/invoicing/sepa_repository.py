"""SQL for SEPA direct debit - migration 0059.

Thin. What is owed is read from `invoicing.invoice_balances`, never recomputed; the rules that
decide which mandate and which sequence type live in `api.invoicing.sepa`. The unique indexes
in 0059 are what actually hold under concurrency, and their violations are translated here so
a lost race answers like the ordinary case rather than as a 500.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.invoicing.sepa import MandateScheme, SequenceKind, SequenceType
from api.invoicing.sepa_service import (
    Candidate,
    CollectionBatch,
    CollectionItem,
    CreditorDetails,
    Mandate,
    MandateReferenceTaken,
    ReservationLost,
)

_MANDATE_COLUMNS = """
    m.id, m.administration_id, m.customer_id, m.mandate_reference, m.scheme, m.sequence_kind,
    m.signed_on, m.debtor_name, m.debtor_iban, m.debtor_bic, m.status, m.created_at,
    m.created_by_user_id, m.revoked_at, m.revoked_reason, m.last_collected_on,
    EXISTS (SELECT 1 FROM sepa_collection c
             WHERE c.mandate_id = m.id AND c.status IN ('submitted', 'collected')) AS used
"""

_BATCH_COLUMNS = """
    id, administration_id, message_id, collection_date, item_count, total_amount,
    file_sha256, created_at, created_by_user_id, cancelled_at
"""

_ITEM_COLUMNS = """
    id, administration_id, batch_id, invoice_id, mandate_id, amount, sequence_type,
    end_to_end_id, status, decided_at, failure_reason, payment_id
"""


def _mandate(row: Any) -> Mandate:
    return Mandate(
        id=row.id,
        administration_id=row.administration_id,
        customer_id=row.customer_id,
        mandate_reference=row.mandate_reference,
        scheme=MandateScheme(row.scheme),
        kind=SequenceKind(row.sequence_kind),
        signed_on=row.signed_on,
        debtor_name=row.debtor_name,
        debtor_iban=row.debtor_iban,
        debtor_bic=row.debtor_bic,
        status=row.status,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
        revoked_at=row.revoked_at,
        revoked_reason=row.revoked_reason,
        last_collected_on=row.last_collected_on,
        used=row.used,
    )


def _batch(row: Any) -> CollectionBatch:
    return CollectionBatch(
        id=row.id,
        administration_id=row.administration_id,
        message_id=row.message_id,
        collection_date=row.collection_date,
        item_count=row.item_count,
        total_amount=Decimal(row.total_amount),
        file_sha256=row.file_sha256,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
        cancelled_at=row.cancelled_at,
    )


def _item(row: Any) -> CollectionItem:
    return CollectionItem(
        id=row.id,
        administration_id=row.administration_id,
        batch_id=row.batch_id,
        invoice_id=row.invoice_id,
        mandate_id=row.mandate_id,
        amount=Decimal(row.amount),
        sequence_type=SequenceType(row.sequence_type),
        end_to_end_id=row.end_to_end_id,
        status=row.status,
        decided_at=row.decided_at,
        failure_reason=row.failure_reason,
        payment_id=row.payment_id,
    )


class SqlSepaRepository:
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

    async def creditor(self, *, administration_id: uuid.UUID) -> CreditorDetails | None:
        result = await self._session.execute(
            text(
                "SELECT coalesce(nullif(btrim(trade_name), ''), legal_name) AS name, "
                "       iban, sepa_creditor_id FROM administration WHERE id = :id"
            ),
            {"id": str(administration_id)},
        )
        row = result.first()
        if row is None:
            return None
        return CreditorDetails(name=row.name, iban=row.iban, creditor_id=row.sepa_creditor_id)

    async def customer_exists(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text("SELECT 1 FROM customer WHERE id = :id AND administration_id = :admin"),
            {"id": str(customer_id), "admin": str(administration_id)},
        )
        return result.first() is not None

    # -- mandates ----------------------------------------
    async def insert_mandate(self, mandate: Mandate) -> Mandate:
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    text(
                        """
                        INSERT INTO sepa_mandate (
                            id, organization_id, administration_id, customer_id,
                            mandate_reference, scheme, sequence_kind, signed_on, debtor_name,
                            debtor_iban, debtor_bic, created_by_user_id
                        ) VALUES (
                            :id,
                            -- overwritten by sepa_mandate_guard(); NOT NULL needs a value.
                            (SELECT organization_id FROM administration WHERE id = :admin),
                            :admin, :customer, :reference, :scheme, :kind, :signed_on,
                            :name, :iban, :bic, :user
                        )
                        """
                    ),
                    {
                        "id": str(mandate.id),
                        "admin": str(mandate.administration_id),
                        "customer": str(mandate.customer_id),
                        "reference": mandate.mandate_reference,
                        "scheme": mandate.scheme.value,
                        "kind": mandate.kind.value,
                        "signed_on": mandate.signed_on,
                        "name": mandate.debtor_name,
                        "iban": mandate.debtor_iban,
                        "bic": mandate.debtor_bic,
                        "user": str(mandate.created_by_user_id),
                    },
                )
        except IntegrityError as exc:
            if "sepa_mandate_reference_idx" in str(exc.orig):
                raise MandateReferenceTaken(mandate.mandate_reference) from exc
            raise
        found = await self.get_mandate(
            administration_id=mandate.administration_id, mandate_id=mandate.id
        )
        assert found is not None
        return found

    async def get_mandate(
        self, *, administration_id: uuid.UUID, mandate_id: uuid.UUID
    ) -> Mandate | None:
        result = await self._session.execute(
            text(
                f"SELECT {_MANDATE_COLUMNS} FROM sepa_mandate m "
                "WHERE m.id = :id AND m.administration_id = :admin"
            ),
            {"id": str(mandate_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _mandate(row)

    async def mandates_for_customer(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> Sequence[Mandate]:
        result = await self._session.execute(
            text(
                f"SELECT {_MANDATE_COLUMNS} FROM sepa_mandate m "
                "WHERE m.customer_id = :customer AND m.administration_id = :admin "
                "ORDER BY m.signed_on DESC, m.created_at DESC"
            ),
            {"customer": str(customer_id), "admin": str(administration_id)},
        )
        return [_mandate(row) for row in result]

    async def revoke_mandate(
        self,
        *,
        administration_id: uuid.UUID,
        mandate_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: str | None,
    ) -> Mandate | None:
        result = await self._session.execute(
            text(
                "UPDATE sepa_mandate SET status = 'revoked', revoked_at = now(), "
                "       revoked_by_user_id = :user, revoked_reason = :reason "
                " WHERE id = :id AND administration_id = :admin AND status = 'active' "
                "RETURNING id"
            ),
            {
                "user": str(user_id),
                "reason": reason,
                "id": str(mandate_id),
                "admin": str(administration_id),
            },
        )
        if result.first() is None:
            return None
        return await self.get_mandate(administration_id=administration_id, mandate_id=mandate_id)

    async def touch_mandate(
        self, *, administration_id: uuid.UUID, mandate_id: uuid.UUID, collected_on: date
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE sepa_mandate SET last_collected_on = :on "
                " WHERE id = :id AND administration_id = :admin AND status = 'active' "
                "   AND (last_collected_on IS NULL OR last_collected_on < :on)"
            ),
            {"on": collected_on, "id": str(mandate_id), "admin": str(administration_id)},
        )

    # -- collecting ----------------------------------------
    async def candidates(
        self, *, administration_id: uuid.UUID, invoice_ids: Sequence[uuid.UUID] | None
    ) -> Sequence[Candidate]:
        # By name: every one of them, so a paid invoice is reported as such. By selection:
        # only what is still owed, and only for a customer who has an active mandate at all.
        by_name = invoice_ids is not None
        result = await self._session.execute(
            text(
                """
                SELECT b.invoice_id, b.invoice_reference, b.customer_id, b.customer_name,
                       b.due_date, b.outstanding,
                       EXISTS (SELECT 1 FROM sepa_collection c
                                WHERE c.invoice_id = b.invoice_id
                                  AND c.status = 'submitted') AS live
                  FROM invoicing.invoice_balances(:admin) b
                 WHERE (CAST(:by_name AS boolean) AND b.invoice_id = ANY(CAST(:ids AS uuid[])))
                    OR (NOT CAST(:by_name AS boolean)
                        AND b.outstanding > 0
                        AND EXISTS (SELECT 1 FROM sepa_mandate m
                                     WHERE m.customer_id = b.customer_id
                                       AND m.status = 'active'))
                 ORDER BY b.due_date NULLS LAST, b.invoice_reference
                """
            ),
            {
                "admin": str(administration_id),
                "by_name": by_name,
                "ids": [str(i) for i in (invoice_ids or [])],
            },
        )
        return [
            Candidate(
                invoice_id=row.invoice_id,
                invoice_reference=row.invoice_reference,
                customer_id=row.customer_id,
                customer_name=row.customer_name,
                due_date=row.due_date,
                outstanding=Decimal(row.outstanding),
                in_live_collection=row.live,
            )
            for row in result
        ]

    async def insert_batch(self, batch: CollectionBatch, file_xml: str) -> CollectionBatch:
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO sepa_collection_batch (
                    id, organization_id, administration_id, message_id, collection_date,
                    item_count, total_amount, file_xml, file_sha256, created_by_user_id
                ) VALUES (
                    :id, (SELECT organization_id FROM administration WHERE id = :admin),
                    :admin, :message, :on, :count, :total, :xml, :sha, :user
                )
                RETURNING {_BATCH_COLUMNS}
                """
            ),
            {
                "id": str(batch.id),
                "admin": str(batch.administration_id),
                "message": batch.message_id,
                "on": batch.collection_date,
                "count": batch.item_count,
                "total": batch.total_amount,
                "xml": file_xml,
                "sha": batch.file_sha256,
                "user": str(batch.created_by_user_id),
            },
        )
        return _batch(result.one())

    async def insert_item(self, item: CollectionItem) -> CollectionItem:
        try:
            async with self._session.begin_nested():
                result = await self._session.execute(
                    text(
                        f"""
                        INSERT INTO sepa_collection (
                            id, organization_id, administration_id, batch_id, invoice_id,
                            mandate_id, amount, sequence_type, end_to_end_id
                        ) VALUES (
                            :id, (SELECT organization_id FROM administration WHERE id = :admin),
                            :admin, :batch, :invoice, :mandate, :amount, :seq, :e2e
                        )
                        RETURNING {_ITEM_COLUMNS}
                        """
                    ),
                    {
                        "id": str(item.id),
                        "admin": str(item.administration_id),
                        "batch": str(item.batch_id),
                        "invoice": str(item.invoice_id),
                        "mandate": str(item.mandate_id),
                        "amount": item.amount,
                        "seq": item.sequence_type.value,
                        "e2e": item.end_to_end_id,
                    },
                )
        except IntegrityError as exc:
            if "sepa_collection_live_invoice_idx" in str(exc.orig):
                raise ReservationLost(str(item.invoice_id)) from exc
            raise
        return _item(result.one())

    async def get_batch(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID
    ) -> CollectionBatch | None:
        result = await self._session.execute(
            text(
                f"SELECT {_BATCH_COLUMNS} FROM sepa_collection_batch "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(batch_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _batch(row)

    async def batch_file(self, *, administration_id: uuid.UUID, batch_id: uuid.UUID) -> str | None:
        result = await self._session.execute(
            text(
                "SELECT file_xml FROM sepa_collection_batch "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(batch_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.file_xml

    async def list_batches(
        self, *, administration_id: uuid.UUID, limit: int
    ) -> Sequence[CollectionBatch]:
        result = await self._session.execute(
            text(
                f"SELECT {_BATCH_COLUMNS} FROM sepa_collection_batch "
                "WHERE administration_id = :admin ORDER BY created_at DESC LIMIT :limit"
            ),
            {"admin": str(administration_id), "limit": limit},
        )
        return [_batch(row) for row in result]

    async def items_for_batch(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID
    ) -> Sequence[CollectionItem]:
        result = await self._session.execute(
            text(
                f"SELECT {_ITEM_COLUMNS} FROM sepa_collection "
                "WHERE batch_id = :batch AND administration_id = :admin ORDER BY end_to_end_id"
            ),
            {"batch": str(batch_id), "admin": str(administration_id)},
        )
        return [_item(row) for row in result]

    async def get_item(
        self, *, administration_id: uuid.UUID, item_id: uuid.UUID
    ) -> CollectionItem | None:
        result = await self._session.execute(
            text(
                f"SELECT {_ITEM_COLUMNS} FROM sepa_collection "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(item_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _item(row)

    async def decide_item(
        self,
        *,
        administration_id: uuid.UUID,
        item_id: uuid.UUID,
        status: str,
        user_id: uuid.UUID,
        failure_reason: str | None,
        payment_id: uuid.UUID | None,
    ) -> CollectionItem | None:
        result = await self._session.execute(
            text(
                f"""
                UPDATE sepa_collection
                   SET status = :status, decided_at = now(), decided_by_user_id = :user,
                       failure_reason = :reason, payment_id = :payment
                 WHERE id = :id AND administration_id = :admin AND status = 'submitted'
                RETURNING {_ITEM_COLUMNS}
                """
            ),
            {
                "status": status,
                "user": str(user_id),
                "reason": failure_reason,
                "payment": None if payment_id is None else str(payment_id),
                "id": str(item_id),
                "admin": str(administration_id),
            },
        )
        row = result.first()
        return None if row is None else _item(row)

    async def cancel_batch(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID, user_id: uuid.UUID
    ) -> CollectionBatch | None:
        # The items first, conditional on still being submitted: if any were decided in the
        # meantime the batch update below finds a mismatch and the savepoint is undone by the
        # caller raising.
        await self._session.execute(
            text(
                "UPDATE sepa_collection SET status = 'cancelled', decided_at = now(), "
                "       decided_by_user_id = :user "
                " WHERE batch_id = :batch AND administration_id = :admin AND status = 'submitted'"
            ),
            {"user": str(user_id), "batch": str(batch_id), "admin": str(administration_id)},
        )
        result = await self._session.execute(
            text(
                f"""
                UPDATE sepa_collection_batch
                   SET cancelled_at = now(), cancelled_by_user_id = :user
                 WHERE id = :id AND administration_id = :admin AND cancelled_at IS NULL
                   AND NOT EXISTS (SELECT 1 FROM sepa_collection c
                                    WHERE c.batch_id = sepa_collection_batch.id
                                      AND c.status IN ('collected', 'failed'))
                RETURNING {_BATCH_COLUMNS}
                """
            ),
            {"user": str(user_id), "id": str(batch_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _batch(row)
