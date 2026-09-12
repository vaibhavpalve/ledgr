"""Value types for the document archive - FR-DOC-001..005, SEC-005.

--- What this module does NOT do ---

It does not enforce the archive's guarantees. Every one of them lives in
migration 0031, as a constraint, a trigger or a withheld privilege:

    unaltered original (001)  partial-immutability trigger on the identity
                              columns; content_hash is what makes it checkable
    retention (002)           derived by trigger from the fiscal year; a
                              caller-supplied value is discarded, and it can be
                              extended but never shortened
    bidirectional link (003)  its own table, because journal_entry is
                              append-only and the relationship is many-to-many
    write-once (005)          no DELETE grant to ledgr_app, plus a BEFORE
                              DELETE trigger that binds ledgr_ops too

The checks here are a fast, well-worded rejection before a round trip, and
nothing more. If this module and the database ever disagree, the database is
right. That is the stance `api.ledger.model` takes for the ledger, for the same
reason: the guarantees have to hold for every writer, and only the database
sees every writer.

--- Size caps are chosen here, not specified anywhere ---

SEC-005 says "size-capped" without giving a number, and the PRD gives none. The
values below are judgements about what FR-EXP-001's capture paths actually
produce - a phone photograph of a receipt, a scanned multi-page invoice - with
enough headroom that a legitimate document is never refused. They are stated as
constants with that reasoning attached rather than buried in a validator,
because the first time one of them is wrong it will be wrong for a real user
holding a real invoice.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from api.documents.content_type import DocumentContentType
from api.documents.retention import RetentionBasis
from api.documents.scanning import ScanStatus

#: A phone photograph at full resolution, with room for a HEIC burst frame.
#: FR-EXP-001d requires the image be "viewable at full resolution", so this
#: cannot be set to whatever makes thumbnails convenient.
MAX_IMAGE_BYTES = 25 * 1024 * 1024

#: A scanned multi-page purchase invoice. Higher than the image cap because
#: FR-EXP-001 names "multi-page PDF" explicitly, and a 40-page scanned annual
#: statement is an ordinary thing to attach.
MAX_PDF_BYTES = 50 * 1024 * 1024

MAX_BYTES: dict[DocumentContentType, int] = {
    DocumentContentType.JPEG: MAX_IMAGE_BYTES,
    DocumentContentType.PNG: MAX_IMAGE_BYTES,
    DocumentContentType.HEIC: MAX_IMAGE_BYTES,
    DocumentContentType.PDF: MAX_PDF_BYTES,
}

#: The largest thing any accepted type permits. Used to refuse an oversized
#: upload BEFORE its bytes are sniffed or hashed - a 2 GB body should not be
#: read into memory to discover it is a 2 GB body.
MAX_ANY_BYTES = max(MAX_BYTES.values())


class DocumentStatus(enum.Enum):
    """Mirrors the `status` CHECK in migration 0031.

    `RESTRICTED` is PRIV-023: an erasure request that fiscal retention law
    blocks does not delete the record, it restricts it - "access limited to
    fiscal purposes, excluded from analytics and search". It is not a softer
    kind of deleted; the document is still there and still retained.
    """

    ACTIVE = "active"
    RESTRICTED = "restricted"


class DocumentError(Exception):
    """Base for this module's refusals."""


class DocumentTooLarge(DocumentError):
    def __init__(self, byte_size: int, limit: int, content_type: str) -> None:
        self.byte_size, self.limit = byte_size, limit
        super().__init__(
            f"{byte_size} bytes exceeds the {limit}-byte limit for {content_type} (SEC-005)"
        )


class DocumentInfected(DocumentError):
    """The scanner reported a detection. The upload is refused and no row is
    written - there is nothing to retain, because nothing was accepted.
    """


class ScanUnavailable(DocumentError):
    """The scanner could not answer, so the upload is refused.

    Fail closed. Accepting unscanned uploads whenever the scanner is down turns
    a dependency outage into an ingestion path for exactly what SEC-005 exists
    to stop, and outages are when nobody is watching.
    """


class DocumentNotFound(DocumentError):
    """No such document in this administration.

    One exception for "does not exist" and "not yours", because under RLS the
    query cannot tell them apart and the API must not appear to.
    """


class DocumentNotReleasable(DocumentError):
    """The document exists and its bytes may not be served yet.

    Carries the scan status so the caller can say something useful, but the
    refusal is the same for `pending`, `failed` and `infected`: an unscanned
    file and an infected one are the same risk to whoever opens them.
    """

    def __init__(self, scan_status: ScanStatus) -> None:
        self.scan_status = scan_status
        super().__init__(f"document is not releasable while its scan is {scan_status.value}")


class NotAuthorizedForDocument(DocumentError):
    def __init__(self, action: str, resource_type: str, detail: str) -> None:
        self.action, self.resource_type, self.detail = action, resource_type, detail
        super().__init__(f"not authorized to {action} {resource_type}: {detail}")


@dataclass(frozen=True, slots=True)
class Document:
    """One row of `document`, as the application reads it.

    The BYTES are deliberately absent. A Document is metadata; obtaining the
    original is a separate call through the blob store, which is what keeps an
    ordinary listing from pulling megabytes out of storage per row.
    """

    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    storage_key: str
    content_hash: bytes
    byte_size: int
    content_type: DocumentContentType
    fiscal_year_id: uuid.UUID
    retention_basis: RetentionBasis
    retention_until: date
    status: DocumentStatus
    scan_status: ScanStatus
    original_filename: str | None = None
    derived_text: str | None = None
    extracted_fields: dict[str, Any] = field(default_factory=dict)
    scanned_at: datetime | None = None
    scanner: str | None = None
    uploaded_by_user_id: uuid.UUID | None = None
    uploaded_at: datetime | None = None

    @property
    def is_releasable(self) -> bool:
        """Whether the original may be served.

        Two conditions, and both are about the reader rather than the record: a
        scan that has cleared (SEC-005), and a status that is not PRIV-023's
        `restricted`, which limits access to fiscal purposes and excludes the
        document from ordinary search and retrieval.
        """
        return self.scan_status.is_downloadable and self.status is DocumentStatus.ACTIVE

    def is_expired(self, today: date) -> bool:
        """Whether retention has run out, inclusive of `retention_until`.

        Mirrors migration 0031's deletion guard, which permits removal only on
        `retention_until < current_date`. The document is held THROUGH its
        retention date.
        """
        return today > self.retention_until


@dataclass(frozen=True, slots=True)
class DocumentPostingLink:
    """FR-DOC-003, one direction of which is as good as the other."""

    id: uuid.UUID
    administration_id: uuid.UUID
    document_id: uuid.UUID
    journal_entry_id: uuid.UUID
    linked_by_user_id: uuid.UUID | None = None
    linked_at: datetime | None = None
    detached_at: datetime | None = None

    @property
    def is_live(self) -> bool:
        return self.detached_at is None


@dataclass(frozen=True, slots=True)
class UnsupportedPosting:
    """FR-DOC-003's completeness report: a posting with no source document.

    Named for what it is rather than `MissingDocument`, because the document is
    not missing - it may never have existed. The report says which postings
    rest on nothing, which is a bookkeeper's month-end question.
    """

    journal_entry_id: uuid.UUID
    administration_id: uuid.UUID
    entry_date: date
    entry_number: int
    description: str
    document_reference: str | None
