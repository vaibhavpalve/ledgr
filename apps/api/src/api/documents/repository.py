"""SQL behind `DocumentRepository` - FR-DOC-001..004.

Every statement here runs on the request's own tenant-scoped session, so RLS
(ADR-003) is what confines it. None of these queries carries a
`WHERE organization_id = ...` for that reason, and the `administration_id`
predicates that ARE here are narrowing within the tenant rather than enforcing
the tenant boundary - the same division `api.main.list_administrations`
documents.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.documents.content_type import DocumentContentType
from api.documents.model import (
    Document,
    DocumentPostingLink,
    DocumentStatus,
    UnsupportedPosting,
)
from api.documents.retention import RetentionBasis
from api.documents.scanning import ScanResult, ScanStatus

#: Every column the application reads back. Named rather than `*`, so a column
#: added later does not silently change what this maps.
_COLUMNS = """
    id, organization_id, administration_id, storage_key, content_hash,
    byte_size, content_type, original_filename, derived_text, extracted_fields,
    fiscal_year_id, retention_basis, retention_until, status, scan_status,
    scanned_at, scanner, uploaded_by_user_id, uploaded_at
"""


def _to_document(row: object) -> Document:
    fields = getattr(row, "extracted_fields", None)
    return Document(
        id=row.id,  # type: ignore[attr-defined]
        organization_id=row.organization_id,  # type: ignore[attr-defined]
        administration_id=row.administration_id,  # type: ignore[attr-defined]
        storage_key=row.storage_key,  # type: ignore[attr-defined]
        content_hash=bytes(row.content_hash),  # type: ignore[attr-defined]
        byte_size=row.byte_size,  # type: ignore[attr-defined]
        content_type=DocumentContentType(row.content_type),  # type: ignore[attr-defined]
        original_filename=row.original_filename,  # type: ignore[attr-defined]
        derived_text=row.derived_text,  # type: ignore[attr-defined]
        # asyncpg returns jsonb already decoded; a driver that returns text is
        # handled rather than assumed, because the difference only shows up in
        # production.
        extracted_fields=json.loads(fields) if isinstance(fields, str) else (fields or {}),
        fiscal_year_id=row.fiscal_year_id,  # type: ignore[attr-defined]
        retention_basis=RetentionBasis(row.retention_basis),  # type: ignore[attr-defined]
        retention_until=row.retention_until,  # type: ignore[attr-defined]
        status=DocumentStatus(row.status),  # type: ignore[attr-defined]
        scan_status=ScanStatus(row.scan_status),  # type: ignore[attr-defined]
        scanned_at=row.scanned_at,  # type: ignore[attr-defined]
        scanner=row.scanner,  # type: ignore[attr-defined]
        uploaded_by_user_id=row.uploaded_by_user_id,  # type: ignore[attr-defined]
        uploaded_at=row.uploaded_at,  # type: ignore[attr-defined]
    )


class SqlDocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        fiscal_year_id: uuid.UUID,
        storage_key: str,
        content_hash: bytes,
        byte_size: int,
        content_type: str,
        original_filename: str | None,
        retention_basis: str,
        scan: ScanResult,
        uploaded_by_user_id: uuid.UUID | None,
    ) -> Document:
        """Insert WITHOUT a retention date.

        `retention_until` is NOT NULL and is not in this statement, which looks
        like an error and is the point: `document_set_retention` (migration
        0031) is a BEFORE INSERT trigger that fills it from the fiscal year and
        discards anything a caller supplied. Sending a value from here would
        make FR-DOC-002's "not deletable by users" depend on this function
        computing it correctly, when the requirement is that no writer gets to
        choose.
        """
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO document (
                    organization_id, administration_id, fiscal_year_id,
                    storage_key, content_hash, byte_size, content_type,
                    original_filename, retention_basis,
                    scan_status, scanned_at, scanner, uploaded_by_user_id,
                    retention_until
                ) VALUES (
                    :organization_id, :administration_id, :fiscal_year_id,
                    :storage_key, :content_hash, :byte_size, :content_type,
                    :original_filename, :retention_basis,
                    :scan_status, now(), :scanner, :uploaded_by_user_id,
                    -- Overwritten by the trigger. A placeholder rather than a
                    -- computed value, so that a trigger which stopped firing
                    -- would produce an obviously wrong date rather than a
                    -- plausible one.
                    'epoch'::date
                )
                RETURNING {_COLUMNS}
                """
            ),
            {
                "organization_id": str(organization_id),
                "administration_id": str(administration_id),
                "fiscal_year_id": str(fiscal_year_id),
                "storage_key": storage_key,
                "content_hash": content_hash,
                "byte_size": byte_size,
                "content_type": content_type,
                "original_filename": original_filename,
                "retention_basis": retention_basis,
                "scan_status": scan.status.value,
                "scanner": scan.scanner,
                "uploaded_by_user_id": str(uploaded_by_user_id) if uploaded_by_user_id else None,
            },
        )
        return _to_document(result.one())

    async def get(self, *, administration_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM document "
                f"WHERE id = :id AND administration_id = :administration_id"
            ),
            {"id": str(document_id), "administration_id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _to_document(row)

    async def link_to_posting(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        document_id: uuid.UUID,
        journal_entry_id: uuid.UUID,
        linked_by_user_id: uuid.UUID | None,
    ) -> DocumentPostingLink:
        """Idempotent in the way FR-DOC-003 needs.

        Linking the same document to the same posting twice is one link, not an
        error: the second attempt is a retry or two people reaching the same
        conclusion, and neither is a conflict. `ON CONFLICT DO NOTHING` against
        the partial unique index over LIVE links gives that, and the follow-up
        SELECT returns the existing row.
        """
        await self._session.execute(
            text(
                """
                INSERT INTO document_posting_link (
                    organization_id, administration_id, document_id,
                    journal_entry_id, linked_by_user_id
                ) VALUES (
                    :organization_id, :administration_id, :document_id,
                    :journal_entry_id, :linked_by_user_id
                )
                ON CONFLICT (document_id, journal_entry_id)
                    WHERE detached_at IS NULL
                DO NOTHING
                """
            ),
            {
                "organization_id": str(organization_id),
                "administration_id": str(administration_id),
                "document_id": str(document_id),
                "journal_entry_id": str(journal_entry_id),
                "linked_by_user_id": str(linked_by_user_id) if linked_by_user_id else None,
            },
        )
        result = await self._session.execute(
            text(
                """
                SELECT id, administration_id, document_id, journal_entry_id,
                       linked_by_user_id, linked_at, detached_at
                  FROM document_posting_link
                 WHERE document_id = :document_id
                   AND journal_entry_id = :journal_entry_id
                   AND detached_at IS NULL
                """
            ),
            {"document_id": str(document_id), "journal_entry_id": str(journal_entry_id)},
        )
        row = result.one()
        return DocumentPostingLink(
            id=row.id,
            administration_id=row.administration_id,
            document_id=row.document_id,
            journal_entry_id=row.journal_entry_id,
            linked_by_user_id=row.linked_by_user_id,
            linked_at=row.linked_at,
            detached_at=row.detached_at,
        )

    async def postings_for(
        self, *, administration_id: uuid.UUID, document_id: uuid.UUID
    ) -> Sequence[uuid.UUID]:
        result = await self._session.execute(
            text(
                "SELECT journal_entry_id FROM document_posting_link "
                "WHERE document_id = :document_id "
                "  AND administration_id = :administration_id "
                "  AND detached_at IS NULL "
                "ORDER BY linked_at"
            ),
            {"document_id": str(document_id), "administration_id": str(administration_id)},
        )
        return [row.journal_entry_id for row in result]

    async def documents_for_posting(
        self, *, administration_id: uuid.UUID, journal_entry_id: uuid.UUID
    ) -> Sequence[uuid.UUID]:
        result = await self._session.execute(
            text(
                "SELECT document_id FROM document_posting_link "
                "WHERE journal_entry_id = :journal_entry_id "
                "  AND administration_id = :administration_id "
                "  AND detached_at IS NULL "
                "ORDER BY linked_at"
            ),
            {
                "journal_entry_id": str(journal_entry_id),
                "administration_id": str(administration_id),
            },
        )
        return [row.document_id for row in result]

    async def postings_without_documents(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID | None
    ) -> Sequence[UnsupportedPosting]:
        result = await self._session.execute(
            text(
                "SELECT journal_entry_id, administration_id, entry_date, "
                "       entry_number, description, document_reference "
                "FROM documents.postings_without_documents("
                "    :administration_id, :fiscal_year_id)"
            ),
            {
                "administration_id": str(administration_id),
                "fiscal_year_id": str(fiscal_year_id) if fiscal_year_id else None,
            },
        )
        return [
            UnsupportedPosting(
                journal_entry_id=row.journal_entry_id,
                administration_id=row.administration_id,
                entry_date=row.entry_date,
                entry_number=row.entry_number,
                description=row.description,
                document_reference=row.document_reference,
            )
            for row in result
        ]

    async def search(
        self, *, administration_id: uuid.UUID, query: str, limit: int
    ) -> Sequence[Document]:
        """FR-DOC-004, over the trigram index on `search_text`.

        `restricted` rows are excluded, which is PRIV-023's "excluded from
        analytics and search" and not an optimisation: a record whose erasure
        was refused on fiscal grounds is reachable for fiscal purposes and must
        not keep surfacing in ordinary use.

        Ordered by similarity so the closest match is first, which is what
        makes type-then-open work on a long archive.
        """
        result = await self._session.execute(
            text(
                f"""
                SELECT {_COLUMNS}
                  FROM document
                 WHERE administration_id = :administration_id
                   AND status = 'active'
                   AND search_text ILIKE :contains
                 ORDER BY similarity(search_text, :query) DESC, uploaded_at DESC
                 LIMIT :limit
                """
            ),
            {
                "administration_id": str(administration_id),
                "contains": f"%{query}%",
                "query": query,
                "limit": limit,
            },
        )
        return [_to_document(row) for row in result]

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    async def restrict(self, *, administration_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        """PRIV-023. `document_original_immutable` (0031) lists the columns
        that may never change on UPDATE, and `status` is not one of them - so
        this UPDATE needs no companion migration to become legal.
        """
        result = await self._session.execute(
            text(
                f"""
                UPDATE document
                   SET status = 'restricted'
                 WHERE id = :id AND administration_id = :administration_id
                RETURNING {_COLUMNS}
                """
            ),
            {"id": str(document_id), "administration_id": str(administration_id)},
        )
        return _to_document(result.one())

    async def record_erasure_request(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        resource_id: uuid.UUID,
        requested_by_user_id: uuid.UUID,
        decision: str,
        explanation: str,
        retained_until: date | None,
    ) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO data_subject_erasure_request (
                    organization_id, administration_id, resource_type, resource_id,
                    requested_by_user_id, decision, explanation, retained_until
                ) VALUES (
                    :organization_id, :administration_id, 'document', :resource_id,
                    :requested_by_user_id, :decision, :explanation, :retained_until
                )
                """
            ),
            {
                "organization_id": str(organization_id),
                "administration_id": str(administration_id),
                "resource_id": str(resource_id),
                "requested_by_user_id": str(requested_by_user_id),
                "decision": decision,
                "explanation": explanation,
                "retained_until": retained_until,
            },
        )
