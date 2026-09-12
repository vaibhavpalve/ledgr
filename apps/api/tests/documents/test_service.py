"""The upload pipeline - FR-DOC-001, FR-DOC-003, SEC-005.

The ORDER of the pipeline is the security control, so most of these tests are
about what has NOT happened when a step refuses: nothing stored, no row, no
blob left behind.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

import pytest

from api.audit.log import AuditLog, AuditOutcome
from api.authz.service import AuthorizationService
from api.documents.content_type import ContentTypeError, DocumentContentType
from api.documents.model import (
    MAX_BYTES,
    Document,
    DocumentInfected,
    DocumentNotFound,
    DocumentNotReleasable,
    DocumentPostingLink,
    DocumentStatus,
    DocumentTooLarge,
    NotAuthorizedForDocument,
    ScanUnavailable,
    UnsupportedPosting,
)
from api.documents.retention import RetentionBasis, retention_until
from api.documents.scanning import (
    EICAR,
    LocalPatternScanner,
    RefusingScanner,
    ScanResult,
    ScanStatus,
)
from api.documents.service import DocumentService, expired_documents
from api.documents.storage import InMemoryBlobStore
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

PDF = b"%PDF-1.7\na receipt\n" + b"0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64

#: The fiscal year every document in these tests is anchored to.
YEAR_END = date(2025, 12, 31)


@dataclass
class FakeDocumentRepository:
    """Enough of the schema to exercise the service, including the one
    behaviour that is really the database's: retention derived from the fiscal
    year rather than taken from the caller.
    """

    organization_id: uuid.UUID
    administration_id: uuid.UUID
    documents: dict[uuid.UUID, Document] = field(default_factory=dict)
    links: list[DocumentPostingLink] = field(default_factory=list)
    unsupported: list[UnsupportedPosting] = field(default_factory=list)

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
        basis = RetentionBasis(retention_basis)
        document = Document(
            id=uuid.uuid4(),
            organization_id=organization_id,
            administration_id=administration_id,
            storage_key=storage_key,
            content_hash=content_hash,
            byte_size=byte_size,
            content_type=DocumentContentType(content_type),
            fiscal_year_id=fiscal_year_id,
            retention_basis=basis,
            # The trigger's job, mirrored here so the service sees what it
            # would really get back.
            retention_until=retention_until(YEAR_END, basis),
            status=DocumentStatus.ACTIVE,
            scan_status=scan.status,
            original_filename=original_filename,
            scanner=scan.scanner,
            scanned_at=datetime.now(),
            uploaded_by_user_id=uploaded_by_user_id,
            uploaded_at=datetime.now(),
        )
        self.documents[document.id] = document
        return document

    async def get(self, *, administration_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        document = self.documents.get(document_id)
        if document is None or document.administration_id != administration_id:
            return None
        return document

    async def link_to_posting(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        document_id: uuid.UUID,
        journal_entry_id: uuid.UUID,
        linked_by_user_id: uuid.UUID | None,
    ) -> DocumentPostingLink:
        for existing in self.links:
            if (
                existing.document_id == document_id
                and existing.journal_entry_id == journal_entry_id
                and existing.is_live
            ):
                return existing
        link = DocumentPostingLink(
            id=uuid.uuid4(),
            administration_id=administration_id,
            document_id=document_id,
            journal_entry_id=journal_entry_id,
            linked_by_user_id=linked_by_user_id,
            linked_at=datetime.now(),
        )
        self.links.append(link)
        return link

    async def postings_for(
        self, *, administration_id: uuid.UUID, document_id: uuid.UUID
    ) -> list[uuid.UUID]:
        return [
            link.journal_entry_id
            for link in self.links
            if link.document_id == document_id and link.is_live
        ]

    async def documents_for_posting(
        self, *, administration_id: uuid.UUID, journal_entry_id: uuid.UUID
    ) -> list[uuid.UUID]:
        return [
            link.document_id
            for link in self.links
            if link.journal_entry_id == journal_entry_id and link.is_live
        ]

    async def postings_without_documents(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID | None
    ) -> list[UnsupportedPosting]:
        return list(self.unsupported)

    async def search(
        self, *, administration_id: uuid.UUID, query: str, limit: int
    ) -> list[Document]:
        return [
            d
            for d in self.documents.values()
            if query.lower() in (d.original_filename or "").lower()
        ][:limit]

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id if administration_id == self.administration_id else None


@dataclass
class Harness:
    service: DocumentService
    repository: FakeDocumentRepository
    blobs: InMemoryBlobStore
    audit: InMemoryAuditRepository
    administration: uuid.UUID
    organization: uuid.UUID
    user: uuid.UUID
    fiscal_year: uuid.UUID


def harness(*, role: str = "Bookkeeper", scanner: Any | None = None) -> Harness:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)

    repository = FakeDocumentRepository(
        organization_id=world.acme, administration_id=world.acme_books
    )
    blobs = InMemoryBlobStore()
    audit = InMemoryAuditRepository()
    return Harness(
        service=DocumentService(
            repository=repository,
            blobs=blobs,
            scanner=scanner or LocalPatternScanner(),
            authorization=AuthorizationService(world.repository),
            audit_log=AuditLog(audit),
        ),
        repository=repository,
        blobs=blobs,
        audit=audit,
        administration=world.acme_books,
        organization=world.acme,
        user=world.user,
        fiscal_year=uuid.uuid4(),
    )


async def recorded(h: Harness, *, action: str | None = None, outcome: Any = None) -> list[Any]:
    """Audit entries, read back the way tests/audit/test_wiring.py reads them -
    through the repository's own search rather than by reaching into its list,
    so these assertions exercise the same path the audit trail itself does.
    """
    entries = await h.audit.search(organization_id=h.organization)
    if action is not None:
        entries = [e for e in entries if e.action == action]
    if outcome is not None:
        entries = [e for e in entries if e.outcome is outcome]
    return list(entries)


async def upload(h: Harness, data: bytes = PDF, **kwargs: Any) -> Document:
    return await h.service.upload(
        administration_id=h.administration,
        fiscal_year_id=h.fiscal_year,
        actor_user_id=h.user,
        data=data,
        **kwargs,
    )


# ===========================================================================
# FR-DOC-001: the original, unaltered
# ===========================================================================


async def test_an_upload_stores_the_original_and_its_hash() -> None:
    h = harness()

    document = await upload(h, original_filename="receipt.pdf")

    assert document.content_hash == hashlib.sha256(PDF).digest()
    assert document.byte_size == len(PDF)
    assert document.content_type is DocumentContentType.PDF
    assert await h.blobs.get(document.storage_key) == PDF


async def test_the_hash_is_of_the_plaintext_not_the_stored_form() -> None:
    """Hashing ciphertext would produce a value that changes when the key
    rotates (SEC-023) - making FR-DOC-001's "unaltered" unverifiable across
    exactly the operation most likely to be blamed for altering something.
    """
    h = harness()
    document = await upload(h)

    assert document.content_hash == hashlib.sha256(PDF).digest()


async def test_reading_it_back_verifies_the_hash() -> None:
    h = harness()
    document = await upload(h)

    _, data = await h.service.original(
        administration_id=h.administration, document_id=document.id, actor_user_id=h.user
    )
    assert data == PDF


async def test_an_altered_original_is_refused_rather_than_served() -> None:
    """A document whose bytes no longer match what was stored is not evidence
    of anything, and handing it to somebody who will treat it as an invoice is
    worse than telling them it is gone.
    """
    h = harness()
    document = await upload(h)

    await h.blobs.put(document.storage_key, b"%PDF-1.7\nsomething else entirely\n")

    with pytest.raises(DocumentNotFound, match="altered or replaced"):
        await h.service.original(
            administration_id=h.administration,
            document_id=document.id,
            actor_user_id=h.user,
        )


# ===========================================================================
# FR-DOC-002: retention
# ===========================================================================


async def test_retention_is_seven_years_from_the_fiscal_year_end() -> None:
    h = harness()

    document = await upload(h)

    assert document.retention_until == date(2032, 12, 31)
    assert document.retention_basis is RetentionBasis.STANDARD


async def test_immovable_property_is_asked_for_not_inferred() -> None:
    h = harness()

    document = await upload(h, retention_basis=RetentionBasis.IMMOVABLE_PROPERTY)

    assert document.retention_until == date(2035, 12, 31)


async def test_the_expiry_sweep_selects_only_documents_past_their_date() -> None:
    h = harness()
    document = await upload(h)

    assert expired_documents([document], date(2032, 12, 31)) == []
    assert expired_documents([document], date(2033, 1, 1)) == [document]


# ===========================================================================
# SEC-005
# ===========================================================================


async def test_a_type_nobody_named_is_refused_and_nothing_is_stored() -> None:
    h = harness()

    with pytest.raises(ContentTypeError):
        await upload(h, b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>")

    assert len(h.blobs) == 0, "the refusal happens before anything is stored"
    assert h.repository.documents == {}


async def test_a_declared_type_that_contradicts_the_bytes_is_refused() -> None:
    h = harness()

    with pytest.raises(ContentTypeError, match="disagrees with itself"):
        await upload(h, PDF, declared_content_type="image/png")

    assert len(h.blobs) == 0


async def test_an_oversized_file_is_refused_against_its_own_types_limit() -> None:
    """The cap is per type: a PNG is held to the image limit even though the
    PDF limit is higher.
    """
    h = harness()
    oversized = PNG + b"0" * MAX_BYTES[DocumentContentType.PNG]

    with pytest.raises(DocumentTooLarge) as raised:
        await upload(h, oversized)

    assert raised.value.limit == MAX_BYTES[DocumentContentType.PNG]
    assert len(h.blobs) == 0


async def test_an_infected_file_is_refused_and_nothing_is_stored() -> None:
    """The scan precedes the store. A store-then-scan pipeline leaves a window
    in which the object exists and nothing has judged it - and the
    compensating delete is the operation FR-DOC-005 forbids.
    """
    h = harness()

    with pytest.raises(DocumentInfected):
        await upload(h, PDF + EICAR)

    assert len(h.blobs) == 0, "nothing was stored"
    assert h.repository.documents == {}, "and there is no row to retain"


async def test_an_infected_upload_is_audited_even_though_it_was_refused() -> None:
    """The refusal is the interesting event. A rejected upload that left no
    trace would make a repeated attempt invisible.
    """
    h = harness()

    with pytest.raises(DocumentInfected):
        await upload(h, PDF + EICAR)

    entries = await recorded(h, action="reject_infected_document")
    assert len(entries) == 1
    assert entries[0].outcome is AuditOutcome.DENIED
    assert entries[0].detail["threat"] == "EICAR-Test-File"
    # The hash, so a repeat of the same file is recognisable in the log.
    assert entries[0].detail["content_sha256"] == hashlib.sha256(PDF + EICAR).hexdigest()


async def test_a_scanner_that_cannot_answer_fails_closed() -> None:
    """Accepting unscanned uploads whenever the scanner is down turns a
    dependency outage into an ingestion path for exactly what SEC-005 exists to
    stop - and outages are when nobody is watching.
    """
    h = harness(scanner=RefusingScanner())

    with pytest.raises(ScanUnavailable):
        await upload(h)

    assert len(h.blobs) == 0


@pytest.mark.parametrize("status", [ScanStatus.PENDING, ScanStatus.FAILED, ScanStatus.INFECTED])
async def test_only_a_clean_document_is_downloadable(status: ScanStatus) -> None:
    """`pending` and `failed` refuse alongside `infected`, which is the only
    safe default: an unscanned file and an infected one are the same risk to
    whoever opens them, and the difference is only time.
    """
    h = harness()
    document = await upload(h)
    h.repository.documents[document.id] = replace(document, scan_status=status)

    with pytest.raises(DocumentNotReleasable):
        await h.service.original(
            administration_id=h.administration,
            document_id=document.id,
            actor_user_id=h.user,
        )


async def test_a_restricted_document_is_not_served() -> None:
    """PRIV-023: a record whose erasure was blocked by retention law has
    "access limited to fiscal purposes" - it is not deleted, and it is not
    handed out on the ordinary path either.
    """
    h = harness()
    document = await upload(h)
    h.repository.documents[document.id] = replace(document, status=DocumentStatus.RESTRICTED)

    with pytest.raises(DocumentNotReleasable):
        await h.service.original(
            administration_id=h.administration,
            document_id=document.id,
            actor_user_id=h.user,
        )


# ===========================================================================
# FR-DOC-003
# ===========================================================================


async def test_a_document_links_to_a_posting_in_both_directions() -> None:
    h = harness()
    document = await upload(h)
    entry = uuid.uuid4()

    await h.service.link_to_posting(
        administration_id=h.administration,
        document_id=document.id,
        journal_entry_id=entry,
        actor_user_id=h.user,
    )

    assert list(
        await h.service.postings_for(
            administration_id=h.administration,
            document_id=document.id,
            actor_user_id=h.user,
        )
    ) == [entry]
    assert list(
        await h.service.documents_for_posting(
            administration_id=h.administration,
            journal_entry_id=entry,
            actor_user_id=h.user,
        )
    ) == [document.id]


async def test_linking_the_same_pair_twice_is_one_link() -> None:
    """A retry, or two people reaching the same conclusion. Neither is a
    conflict, and NFR-032's retries must not produce a second link.
    """
    h = harness()
    document = await upload(h)
    entry = uuid.uuid4()

    first = await h.service.link_to_posting(
        administration_id=h.administration,
        document_id=document.id,
        journal_entry_id=entry,
        actor_user_id=h.user,
    )
    second = await h.service.link_to_posting(
        administration_id=h.administration,
        document_id=document.id,
        journal_entry_id=entry,
        actor_user_id=h.user,
    )

    assert first.id == second.id
    assert len(h.repository.links) == 1


async def test_linking_a_document_that_does_not_exist_says_so() -> None:
    h = harness()

    with pytest.raises(DocumentNotFound):
        await h.service.link_to_posting(
            administration_id=h.administration,
            document_id=uuid.uuid4(),
            journal_entry_id=uuid.uuid4(),
            actor_user_id=h.user,
        )


async def test_the_completeness_report_lists_postings_resting_on_nothing() -> None:
    h = harness()
    h.repository.unsupported.append(
        UnsupportedPosting(
            journal_entry_id=uuid.uuid4(),
            administration_id=h.administration,
            entry_date=date(2025, 6, 1),
            entry_number=42,
            description="Onkosten zonder bon",
            document_reference=None,
        )
    )

    report = await h.service.completeness_report(
        administration_id=h.administration, actor_user_id=h.user
    )

    assert len(report) == 1
    assert report[0].entry_number == 42


# ===========================================================================
# Authorization and tenancy
# ===========================================================================


async def test_a_role_without_the_capability_cannot_upload() -> None:
    """`upload document` is one of matrix.py's EXTENSION_CAPABILITIES, held by
    the roles IAM-101's capture profiles name. A Viewer is not one of them.
    """
    h = harness(role="Viewer")

    with pytest.raises(NotAuthorizedForDocument):
        await upload(h)

    assert len(h.blobs) == 0


async def test_a_denied_upload_is_audited() -> None:
    h = harness(role="Viewer")

    with pytest.raises(NotAuthorizedForDocument):
        await upload(h)

    denied = await recorded(h, outcome=AuditOutcome.DENIED)
    assert denied and denied[0].action == "upload_document"


async def test_another_administrations_document_is_not_found() -> None:
    """ "Does not exist" and "not yours" are the same answer: RLS filters the
    row out either way, so the service cannot tell them apart and must not
    appear to.
    """
    h = harness()
    document = await upload(h)

    with pytest.raises(DocumentNotFound):
        await h.service.metadata(
            administration_id=uuid.uuid4(),
            document_id=document.id,
            actor_user_id=h.user,
        )


async def test_a_successful_download_is_audited_as_a_financial_read() -> None:
    """IAM-090. Taking a copy of the evidence a posting rests on is the read an
    auditor asks about; listing a filename is not, and auditing both would
    drown the first in the second.
    """
    from api.audit.log import AuditCategory

    h = harness()
    document = await upload(h)

    await h.service.original(
        administration_id=h.administration, document_id=document.id, actor_user_id=h.user
    )

    downloads = await recorded(h, action="download_document")
    assert len(downloads) == 1
    assert downloads[0].category is AuditCategory.FINANCIAL_READ

    await h.service.metadata(
        administration_id=h.administration, document_id=document.id, actor_user_id=h.user
    )
    assert len(await recorded(h, action="download_document")) == 1


async def test_search_returns_nothing_for_an_empty_query() -> None:
    h = harness()
    await upload(h, original_filename="receipt.pdf")

    assert (
        await h.service.search(
            administration_id=h.administration, actor_user_id=h.user, query="   "
        )
        == []
    )
