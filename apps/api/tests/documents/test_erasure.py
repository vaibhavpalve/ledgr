"""api.documents.service.DocumentService.request_erasure - PRIV-022/PRIV-023,
without a database.

What migration 0031 already guarantees (retention derivation, the deletion
guard, search excluding `restricted`) is exercised in tests/documents/
test_retention.py and tests/documents/test_service.py; this is about the
DECISION the service makes given a document's current retention state, and
what it records.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.documents.content_type import DocumentContentType
from api.documents.model import Document, DocumentNotFound, DocumentStatus
from api.documents.retention import RetentionBasis
from api.documents.scanning import ScanStatus
from api.documents.service import DocumentService
from api.documents.storage import InMemoryBlobStore
from api.privacy.model import ErasureOutcome
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

pytestmark = pytest.mark.anyio


def _document(
    *,
    administration_id: uuid.UUID,
    organization_id: uuid.UUID,
    retention_until: date,
    status: DocumentStatus = DocumentStatus.ACTIVE,
) -> Document:
    return Document(
        id=uuid.uuid4(),
        organization_id=organization_id,
        administration_id=administration_id,
        storage_key=f"{administration_id}/doc",
        content_hash=bytes(32),
        byte_size=128,
        content_type=DocumentContentType.PDF,
        fiscal_year_id=uuid.uuid4(),
        retention_basis=RetentionBasis.STANDARD,
        retention_until=retention_until,
        status=status,
        scan_status=ScanStatus.CLEAN,
        uploaded_at=datetime.now(),
    )


@dataclass
class FakeDocumentRepository:
    """Only what `request_erasure` calls: `get`, `restrict`, `organization_of`
    and `record_erasure_request`. The other DocumentRepository methods are
    unused by this path, and a Protocol is not checked at runtime - the same
    minimalism `tests/documents/test_retention_job.py` uses for its own fake.
    """

    documents: dict[uuid.UUID, Document] = field(default_factory=dict)
    organizations: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    erasure_requests: list[dict[str, object]] = field(default_factory=list)

    async def get(self, *, administration_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        document = self.documents.get(document_id)
        if document is None or document.administration_id != administration_id:
            return None
        return document

    async def restrict(self, *, administration_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        existing = self.documents[document_id]
        updated = replace(existing, status=DocumentStatus.RESTRICTED)
        self.documents[document_id] = updated
        return updated

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organizations.get(administration_id)

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
        self.erasure_requests.append(
            {
                "organization_id": organization_id,
                "administration_id": administration_id,
                "resource_id": resource_id,
                "requested_by_user_id": requested_by_user_id,
                "decision": decision,
                "explanation": explanation,
                "retained_until": retained_until,
            }
        )


@dataclass
class Harness:
    service: DocumentService
    repository: FakeDocumentRepository
    administration: uuid.UUID
    organization: uuid.UUID
    user: uuid.UUID


def harness(*, role: str = "Bookkeeper") -> Harness:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)

    repository = FakeDocumentRepository()
    repository.organizations[world.acme_books] = world.acme

    return Harness(
        service=DocumentService(
            repository=repository,  # type: ignore[arg-type]
            blobs=InMemoryBlobStore(),
            scanner=None,  # type: ignore[arg-type]
            authorization=AuthorizationService(world.repository),
            audit_log=AuditLog(InMemoryAuditRepository()),  # type: ignore[arg-type]
        ),
        repository=repository,
        administration=world.acme_books,
        organization=world.acme,
        user=world.user,
    )


async def test_a_document_still_inside_retention_is_restricted_not_erased() -> None:
    h = harness()
    document = _document(
        administration_id=h.administration,
        organization_id=h.organization,
        retention_until=date(2032, 12, 31),
    )
    h.repository.documents[document.id] = document

    decision = await h.service.request_erasure(
        administration_id=h.administration,
        document_id=document.id,
        actor_user_id=h.user,
        today=date(2026, 9, 13),
    )

    assert decision.outcome is ErasureOutcome.RESTRICTED
    assert decision.retained_until == date(2032, 12, 31)
    assert "2032-12-31" in decision.explanation
    assert h.repository.documents[document.id].status is DocumentStatus.RESTRICTED


async def test_a_document_already_past_retention_is_erased_with_no_restriction() -> None:
    h = harness()
    document = _document(
        administration_id=h.administration,
        organization_id=h.organization,
        retention_until=date(2020, 1, 1),
    )
    h.repository.documents[document.id] = document

    decision = await h.service.request_erasure(
        administration_id=h.administration,
        document_id=document.id,
        actor_user_id=h.user,
        today=date(2026, 9, 13),
    )

    assert decision.outcome is ErasureOutcome.ERASED
    assert decision.retained_until is None
    # Nothing to restrict - the next PRIV-030 sweep is what removes it.
    assert h.repository.documents[document.id].status is DocumentStatus.ACTIVE


async def test_the_boundary_day_itself_is_still_retained() -> None:
    """`is_expired` is strict: retained THROUGH retention_until, collectable
    the day after - mirrors migration 0031's deletion guard.
    """
    h = harness()
    document = _document(
        administration_id=h.administration,
        organization_id=h.organization,
        retention_until=date(2026, 9, 13),
    )
    h.repository.documents[document.id] = document

    decision = await h.service.request_erasure(
        administration_id=h.administration,
        document_id=document.id,
        actor_user_id=h.user,
        today=date(2026, 9, 13),
    )

    assert decision.outcome is ErasureOutcome.RESTRICTED


async def test_the_decision_is_recorded_with_its_explanation() -> None:
    h = harness()
    document = _document(
        administration_id=h.administration,
        organization_id=h.organization,
        retention_until=date(2032, 12, 31),
    )
    h.repository.documents[document.id] = document

    await h.service.request_erasure(
        administration_id=h.administration,
        document_id=document.id,
        actor_user_id=h.user,
        today=date(2026, 9, 13),
    )

    assert len(h.repository.erasure_requests) == 1
    recorded = h.repository.erasure_requests[0]
    assert recorded["decision"] == "restricted"
    assert recorded["retained_until"] == date(2032, 12, 31)
    assert recorded["resource_id"] == document.id


async def test_an_unknown_document_is_refused() -> None:
    h = harness()

    with pytest.raises(DocumentNotFound):
        await h.service.request_erasure(
            administration_id=h.administration,
            document_id=uuid.uuid4(),
            actor_user_id=h.user,
        )


async def test_another_administrations_document_is_not_found() -> None:
    """Indistinguishable from "does not exist" - the same posture every other
    document lookup takes.
    """
    h = harness()
    document = _document(
        administration_id=h.administration,
        organization_id=h.organization,
        retention_until=date(2032, 12, 31),
    )
    h.repository.documents[document.id] = document

    with pytest.raises(DocumentNotFound):
        await h.service.request_erasure(
            administration_id=uuid.uuid4(),
            document_id=document.id,
            actor_user_id=h.user,
        )
