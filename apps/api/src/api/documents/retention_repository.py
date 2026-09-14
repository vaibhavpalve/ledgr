"""SQL behind `DocumentRetentionRepository` - `documents.expired()`
(migration 0045) and the DELETE that removes an expired row.

Unlike `api.ledger.integrity_repository.SqlIntegrityRepository` (read-only,
so either role can run it), `delete()` below only ever works as `ledgr_ops`:
migration 0031 grants DELETE on `document` to ledgr_ops alone and explicitly
withholds it from ledgr_app - "FR-DOC-002's 'not deletable by users' is this
line: the application role ... has no way to express the statement" - so a
connection authenticated as ledgr_app fails this with permission denied
regardless of how expired the row is. `expired()` (SELECT) would work as
either, but there is no on-demand ledgr_app mode here to support it for; see
scripts/enforce_document_retention.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.documents.retention_job import ExpiredDocument


class SqlDocumentRetentionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def expired(
        self, *, administration_id: uuid.UUID | None = None
    ) -> Sequence[ExpiredDocument]:
        result = await self._session.execute(
            text(
                "SELECT id, organization_id, administration_id, retention_basis, "
                "       retention_until, status, blocking_link_count "
                "  FROM documents.expired(:administration_id)"
            ),
            {"administration_id": str(administration_id) if administration_id else None},
        )
        return [
            ExpiredDocument(
                id=row.id,
                organization_id=row.organization_id,
                administration_id=row.administration_id,
                retention_basis=row.retention_basis,
                retention_until=row.retention_until,
                status=row.status,
                blocking_link_count=int(row.blocking_link_count),
            )
            for row in result
        ]

    async def delete(self, *, document_id: uuid.UUID) -> None:
        """Unconditional. `document_deletion_guard` (0031) is what makes this
        safe to send without a WHERE on `retention_until` here too - the
        database re-checks it, and the caller (`DocumentRetentionSweepJob`)
        has already filtered out anything `documents.expired()` marked as
        blocked by a surviving link.
        """
        await self._session.execute(
            text("DELETE FROM document WHERE id = :id"), {"id": str(document_id)}
        )
