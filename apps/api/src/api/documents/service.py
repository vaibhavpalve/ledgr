"""The document archive's entry point - FR-DOC-001..005, SEC-005.

--- The order of the upload pipeline is the security control ---

    1. authorize            before anything is read
    2. cap the size         before the bytes are examined
    3. sniff the type       from the bytes, never the filename (SEC-005)
    4. hash                 the ORIGINAL bytes, before encryption
    5. scan                 and refuse anything not clean (SEC-005)
    6. store                encrypted under the administration's own key
    7. record               the row, whose retention the database derives
    8. audit                IAM-090

Each step is placed where it is for a reason, and several of them would be
wrong one position later:

  * The size cap precedes the sniff, so a body far too large to be a document
    is refused without being examined at all.
  * The hash is taken of the plaintext, before encryption. Hashing ciphertext
    would produce a value that changes when the key rotates (SEC-023), which
    would make FR-DOC-001's "unaltered" unverifiable across exactly the
    operation most likely to be blamed for altering something.
  * The scan precedes the store. A store-then-scan pipeline leaves a window in
    which the object exists and nothing has judged it, and the compensating
    delete is the operation FR-DOC-005 forbids.
  * The row is written last, so a failure leaves an orphan BLOB rather than a
    row pointing at nothing. An orphan blob is collected by a sweep; a row
    whose original is missing is a lost document, which is the failure this
    whole module exists to prevent.

--- What this service does not decide ---

Retention. The row is inserted without one and the database derives it from the
fiscal year (migration 0031's `document_set_retention` trigger), because
FR-DOC-002's "not deletable by users" has to be true of this service too. A
service that computed retention and sent it would be a service that could send
a shorter one.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import (
    AdministrationScope,
    AuthorizationRequest,
    ResourceAttributes,
)
from api.authz.service import AuthorizationService
from api.documents.content_type import verify_declared
from api.documents.model import (
    MAX_ANY_BYTES,
    MAX_BYTES,
    Document,
    DocumentInfected,
    DocumentNotFound,
    DocumentNotReleasable,
    DocumentPostingLink,
    DocumentTooLarge,
    NotAuthorizedForDocument,
    ScanUnavailable,
    UnsupportedPosting,
)
from api.documents.retention import RetentionBasis
from api.documents.scanning import MalwareScanner, ScanResult, ScanStatus
from api.documents.storage import BlobStore, new_storage_key
from api.privacy.model import ErasureDecision, ErasureOutcome

#: matrix.py's EXTENSION_CAPABILITIES, which exist because IAM-101's "Capture
#: only" profile needs them and Appendix A grades neither documents nor
#: customers. Reused rather than invented, per ADR-012.
UPLOAD_DOCUMENT = ("upload", "document")
VIEW_DOCUMENT = ("view", "document")


class DocumentRepository(Protocol):
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
    ) -> Document: ...

    async def get(
        self, *, administration_id: uuid.UUID, document_id: uuid.UUID
    ) -> Document | None: ...

    async def link_to_posting(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        document_id: uuid.UUID,
        journal_entry_id: uuid.UUID,
        linked_by_user_id: uuid.UUID | None,
    ) -> DocumentPostingLink: ...

    async def postings_for(
        self, *, administration_id: uuid.UUID, document_id: uuid.UUID
    ) -> Sequence[uuid.UUID]: ...

    async def documents_for_posting(
        self, *, administration_id: uuid.UUID, journal_entry_id: uuid.UUID
    ) -> Sequence[uuid.UUID]: ...

    async def postings_without_documents(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID | None
    ) -> Sequence[UnsupportedPosting]: ...

    async def search(
        self, *, administration_id: uuid.UUID, query: str, limit: int
    ) -> Sequence[Document]: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def restrict(
        self, *, administration_id: uuid.UUID, document_id: uuid.UUID
    ) -> Document: ...

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
    ) -> None: ...


class DocumentService:
    """FR-DOC's entry point. One instance per request, already scoped to the
    administration by the blob store it is handed.
    """

    def __init__(
        self,
        repository: DocumentRepository,
        blobs: BlobStore,
        scanner: MalwareScanner,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._blobs = blobs
        self._scanner = scanner
        self._authorization = authorization
        self._audit = audit_log

    # -- FR-DOC-001, SEC-005 ------------------------------------------------

    async def upload(
        self,
        *,
        administration_id: uuid.UUID,
        fiscal_year_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        data: bytes,
        original_filename: str | None = None,
        declared_content_type: str | None = None,
        retention_basis: RetentionBasis = RetentionBasis.STANDARD,
        correlation_id: str | None = None,
    ) -> Document:
        """Store an original, unaltered, and return its record.

        `retention_basis` is a caller's declaration and defaults to STANDARD.
        Whether a document relates to immovable property is an accounting
        judgement about the transaction behind it, not a fact in the file, so
        it is asked for rather than inferred - and the default is the shorter
        period only because the longer one can be applied later and the trigger
        in 0031 refuses to shorten it back.
        """
        await self._require(
            UPLOAD_DOCUMENT, user_id=actor_user_id, administration_id=administration_id
        )

        # Before anything reads the bytes. A body far too large to be any
        # accepted document is refused without being sniffed or hashed.
        if len(data) > MAX_ANY_BYTES:
            raise DocumentTooLarge(len(data), MAX_ANY_BYTES, "any document type")

        content_type = verify_declared(data, declared_content_type)

        limit = MAX_BYTES[content_type]
        if len(data) > limit:
            raise DocumentTooLarge(len(data), limit, content_type.value)

        # Of the ORIGINAL bytes, before encryption - see the module docstring.
        content_hash = hashlib.sha256(data).digest()

        scan = await self._scanner.scan(data)
        if scan.status is ScanStatus.INFECTED:
            await self._record_event(
                administration_id=administration_id,
                user_id=actor_user_id,
                action="reject_infected_document",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                correlation_id=correlation_id,
                detail={
                    "scanner": scan.scanner,
                    "threat": scan.detail,
                    "byte_size": len(data),
                    "content_type": content_type.value,
                    "content_sha256": content_hash.hex(),
                },
            )
            raise DocumentInfected(
                f"{scan.scanner} reported a detection; the upload was refused and "
                f"nothing was stored"
            )
        if scan.status is not ScanStatus.CLEAN:
            raise ScanUnavailable(
                f"{scan.scanner} could not scan this file ({scan.detail}). The "
                f"upload is refused rather than stored unscanned (SEC-005)."
            )

        storage_key = new_storage_key(administration_id)
        await self._blobs.put(storage_key, data)

        organization_id = await self._organization_of(administration_id)
        document = await self._repository.record(
            organization_id=organization_id,
            administration_id=administration_id,
            fiscal_year_id=fiscal_year_id,
            storage_key=storage_key,
            content_hash=content_hash,
            byte_size=len(data),
            content_type=content_type.value,
            original_filename=original_filename,
            retention_basis=retention_basis.value,
            scan=scan,
            uploaded_by_user_id=actor_user_id,
        )

        await self._record_event(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="upload_document",
            resource_id=document.id,
            correlation_id=correlation_id,
            detail={
                "content_type": document.content_type.value,
                "byte_size": document.byte_size,
                # The hash, so the audit trail can answer "is the original in
                # the archive today the one that was uploaded" without the
                # archive being the only witness to itself.
                "content_sha256": content_hash.hex(),
                "retention_until": document.retention_until.isoformat(),
                "retention_basis": document.retention_basis.value,
                "scanner": scan.scanner,
            },
        )
        return document

    # -- FR-DOC-001: reading the original back ------------------------------

    async def original(
        self, *, administration_id: uuid.UUID, document_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> tuple[Document, bytes]:
        """The stored original, with its record.

        Verifies the hash on the way out. That is not paranoia about the
        storage provider so much as the only place FR-DOC-001's "unaltered" can
        actually be observed: the database can guarantee the hash column has
        not changed, and only a read can tell whether the bytes still match it.
        """
        document = await self._read(
            administration_id=administration_id,
            document_id=document_id,
            actor_user_id=actor_user_id,
        )

        if not document.is_releasable:
            raise DocumentNotReleasable(document.scan_status)

        data = await self._blobs.get(document.storage_key)

        # IAM-090's "data reads of financial records", recorded here and not on
        # `metadata`. Taking a COPY of the evidence a posting rests on is the
        # read an auditor asks about; listing a filename is not, and auditing
        # both would drown the first in the second.
        await self._record_event(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="download_document",
            resource_id=document.id,
            category=AuditCategory.FINANCIAL_READ,
            detail={
                "content_type": document.content_type.value,
                "byte_size": document.byte_size,
            },
        )

        if hashlib.sha256(data).digest() != document.content_hash:
            # Deliberately not "return it anyway with a warning". A document
            # whose bytes no longer match what was stored is not evidence of
            # anything, and handing it to somebody who will treat it as an
            # invoice is worse than telling them it is gone.
            raise DocumentNotFound(
                f"document {document_id} no longer matches the hash recorded at "
                f"upload; the original has been altered or replaced (FR-DOC-001)"
            )
        return document, data

    async def metadata(
        self, *, administration_id: uuid.UUID, document_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Document:
        return await self._read(
            administration_id=administration_id,
            document_id=document_id,
            actor_user_id=actor_user_id,
        )

    # -- FR-DOC-003 --------------------------------------------------------

    async def link_to_posting(
        self,
        *,
        administration_id: uuid.UUID,
        document_id: uuid.UUID,
        journal_entry_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> DocumentPostingLink:
        """Attach a document to a posting.

        Requires `upload document` rather than `view document`: linking asserts
        that this file is the evidence for that entry, which is a claim about
        the books, not a way of reading them.
        """
        await self._require(
            UPLOAD_DOCUMENT, user_id=actor_user_id, administration_id=administration_id
        )
        # Existence is checked here so the caller gets "no such document"
        # rather than a foreign key error, and so the check happens under the
        # same RLS predicate the insert will.
        if (
            await self._repository.get(administration_id=administration_id, document_id=document_id)
            is None
        ):
            raise DocumentNotFound(f"document {document_id} not found")

        organization_id = await self._organization_of(administration_id)
        link = await self._repository.link_to_posting(
            organization_id=organization_id,
            administration_id=administration_id,
            document_id=document_id,
            journal_entry_id=journal_entry_id,
            linked_by_user_id=actor_user_id,
        )

        await self._record_event(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="link_document_to_posting",
            resource_id=document_id,
            correlation_id=correlation_id,
            detail={"journal_entry_id": str(journal_entry_id), "link_id": str(link.id)},
        )
        return link

    async def postings_for(
        self, *, administration_id: uuid.UUID, document_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Sequence[uuid.UUID]:
        """One direction of FR-DOC-003's bidirectional link."""
        await self._require(
            VIEW_DOCUMENT, user_id=actor_user_id, administration_id=administration_id
        )
        return await self._repository.postings_for(
            administration_id=administration_id, document_id=document_id
        )

    async def documents_for_posting(
        self,
        *,
        administration_id: uuid.UUID,
        journal_entry_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> Sequence[uuid.UUID]:
        """The other direction, and the reason the link is its own table."""
        await self._require(
            VIEW_DOCUMENT, user_id=actor_user_id, administration_id=administration_id
        )
        return await self._repository.documents_for_posting(
            administration_id=administration_id, journal_entry_id=journal_entry_id
        )

    async def completeness_report(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        fiscal_year_id: uuid.UUID | None = None,
    ) -> Sequence[UnsupportedPosting]:
        """FR-DOC-003: "a posting without a source document is flagged in a
        completeness report".

        Requires `view document`, not a ledger permission: the report is about
        which evidence is absent, and somebody who may see the documents may
        see which ones are missing.
        """
        await self._require(
            VIEW_DOCUMENT, user_id=actor_user_id, administration_id=administration_id
        )
        return await self._repository.postings_without_documents(
            administration_id=administration_id, fiscal_year_id=fiscal_year_id
        )

    # -- FR-DOC-004 --------------------------------------------------------

    async def search(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        query: str,
        limit: int = 50,
    ) -> Sequence[Document]:
        """FR-DOC-004, over derived text, filename and extracted supplier.

        Filtering happens in the query rather than after it, for the reason
        `api.firm.switcher` gives about the client switcher: a search that
        fetched everything and hid some would have already put the hidden rows
        in a response body.
        """
        await self._require(
            VIEW_DOCUMENT, user_id=actor_user_id, administration_id=administration_id
        )
        stripped = query.strip()
        if not stripped:
            return []
        return await self._repository.search(
            administration_id=administration_id, query=stripped, limit=limit
        )

    # -- PRIV-022/PRIV-023 ---------------------------------------------------

    async def request_erasure(
        self,
        *,
        administration_id: uuid.UUID,
        document_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
        today: date | None = None,
    ) -> ErasureDecision:
        """PRIV-022: honour an erasure request, or explain why it is blocked.

        Requires `upload document` rather than a new permission: ADR-012
        forbids inventing one, and this is at least as privileged an act on a
        document as `link_to_posting`, which already reuses it.

        Every live document sits inside FR-DOC-002's retention window until
        `retention_until`, so the ordinary answer is PRIV-023's RESTRICTED:
        the row stays, `status` becomes `restricted` (which
        `DocumentRepository.search` already excludes from ordinary use), and
        it is swept for removal once retention actually ends
        (scripts/enforce_document_retention.py, PRIV-030). A document already
        past its `retention_until` needs no restriction - it is ERASED without
        one, and the next sweep is what removes it: this service runs as
        `ledgr_app`, which migration 0031 deliberately grants no DELETE on
        `document` (see `documents.delete_with_approval`'s comment - "a
        request handler must not be able to delete a document even with an
        approval row in front of it"), so a synchronous delete here is not a
        capability this method is given, by design.

        `today` is a parameter for the reason `api.documents.retention.
        is_expired` gives one: the boundary is the only interesting part of
        this rule, and a rule that reads the clock cannot be tested at it.
        """
        await self._require(
            UPLOAD_DOCUMENT, user_id=actor_user_id, administration_id=administration_id
        )
        document = await self._repository.get(
            administration_id=administration_id, document_id=document_id
        )
        if document is None:
            raise DocumentNotFound(f"document {document_id} not found")

        as_of = today if today is not None else date.today()
        decided_at = datetime.now(UTC)

        if document.is_expired(as_of):
            outcome = ErasureOutcome.ERASED
            retained_until: date | None = None
            explanation = (
                f"retention under FR-DOC-002 expired on "
                f"{document.retention_until.isoformat()}; no restriction is needed "
                "and this document will be removed by the next scheduled retention "
                "sweep (PRIV-030)."
            )
        else:
            outcome = ErasureOutcome.RESTRICTED
            retained_until = document.retention_until
            explanation = (
                f"retained under Dutch fiscal law until "
                f"{document.retention_until.isoformat()} "
                f"({document.retention_basis.years} years from the end of the fiscal "
                "year this document belongs to, FR-DOC-002). The document is "
                "restricted rather than erased: access is limited to fiscal "
                "purposes, it is excluded from search and analytics, and it will be "
                f"deleted automatically after {document.retention_until.isoformat()} "
                "(PRIV-030)."
            )
            await self._repository.restrict(
                administration_id=administration_id, document_id=document_id
            )

        organization_id = await self._organization_of(administration_id)
        await self._repository.record_erasure_request(
            organization_id=organization_id,
            administration_id=administration_id,
            resource_id=document_id,
            requested_by_user_id=actor_user_id,
            decision=outcome.value,
            explanation=explanation,
            retained_until=retained_until,
        )

        await self._record_event(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="request_document_erasure",
            resource_id=document_id,
            correlation_id=correlation_id,
            detail={
                "outcome": outcome.value,
                "retained_until": retained_until.isoformat() if retained_until else None,
            },
        )

        return ErasureDecision(
            resource_type="document",
            resource_id=document_id,
            outcome=outcome,
            explanation=explanation,
            retained_until=retained_until,
            decided_at=decided_at,
        )

    # -- internals ---------------------------------------------------------

    async def _read(
        self, *, administration_id: uuid.UUID, document_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Document:
        await self._require(
            VIEW_DOCUMENT, user_id=actor_user_id, administration_id=administration_id
        )
        document = await self._repository.get(
            administration_id=administration_id, document_id=document_id
        )
        if document is None:
            # "Does not exist" and "belongs to another tenant" are the same
            # answer: RLS filters the row out of the query either way, so this
            # branch genuinely cannot tell them apart and must not appear to.
            raise DocumentNotFound(f"document {document_id} not found")
        return document

    async def _organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise DocumentNotFound(f"administration {administration_id} does not exist")
        return organization_id

    async def _require(
        self,
        permission: tuple[str, str],
        *,
        user_id: uuid.UUID,
        administration_id: uuid.UUID,
    ) -> None:
        action, resource_type = permission
        decision = await self._authorization.authorize(
            AuthorizationRequest(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                target=AdministrationScope(administration_id),
                attributes=ResourceAttributes(),
            )
        )
        if not decision.allowed:
            await self._record_event(
                administration_id=administration_id,
                user_id=user_id,
                action=f"{action}_{resource_type}",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise NotAuthorizedForDocument(
                action, resource_type, decision.detail or decision.reason
            )

    async def _record_event(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        resource_id: uuid.UUID,
        detail: dict[str, Any] | None = None,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        correlation_id: str | None = None,
        # Stated by each call site rather than guessed from the action's name.
        # A prefix test looks tidy and is wrong the first time an action is
        # named `download_...` instead of `view_...` - which is exactly the
        # event IAM-090's "data reads of financial records" most wants.
        category: AuditCategory = AuditCategory.CONFIGURATION,
    ) -> None:
        organization_id = await self._organization_of(administration_id)
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=category,
                action=action,
                resource_type="document",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=dict(detail or {}),
            )
        )


def expired_documents(documents: Sequence[Document], today: date) -> list[Document]:
    """PRIV-030's sweep, as a pure function over rows.

    Separated from the job that deletes so the SELECTION can be tested at the
    boundary without anything being removed. The boundary is the only
    interesting part: a document is retained THROUGH `retention_until` and is
    collectable the day after.
    """
    return [document for document in documents if document.is_expired(today)]
