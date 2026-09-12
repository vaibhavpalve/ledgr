"""The document archive - PRD §6.10 (FR-DOC) and SEC-005.

    content_type.py  SEC-005's first clause: what a file IS, decided from its
                     bytes and never from its name.
    scanning.py      SEC-005's malware scanning, as a state on the row rather
                     than a boolean in a function.
    storage.py       where the bytes live - the provider seam (non-negotiable
                     #4), with per-administration encryption above it.
    retention.py     FR-DOC-002's rule, mirrored by documents.retention_until()
                     in migration 0031 and compared against one case table.
    model.py         the value types, and a note on which guarantees are the
                     database's rather than this layer's.
    service.py       the upload pipeline, whose ORDER is the security control.
    repository.py    the SQL, running under the request's own RLS session.

Everything that makes the archive trustworthy is in migration 0031: the
partial-immutability trigger that keeps an original unaltered while letting OCR
results land beside it, the trigger that derives retention from the fiscal year
and discards whatever a caller supplied, the withheld DELETE grant, and the
deletion guard that binds ledgr_ops too. This package is the well-worded
refusal that happens first.
"""

from api.documents.content_type import (
    SIGNATURES,
    ContentTypeError,
    DocumentContentType,
    sniff,
    verify_declared,
)
from api.documents.model import (
    MAX_ANY_BYTES,
    MAX_BYTES,
    Document,
    DocumentError,
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
from api.documents.repository import SqlDocumentRepository
from api.documents.retention import RetentionBasis, add_years, is_expired, retention_until
from api.documents.scanning import (
    EICAR,
    LocalPatternScanner,
    MalwareScanner,
    RefusingScanner,
    ScanResult,
    ScanStatus,
    build_scanner,
)
from api.documents.service import (
    UPLOAD_DOCUMENT,
    VIEW_DOCUMENT,
    DocumentRepository,
    DocumentService,
    expired_documents,
)
from api.documents.storage import (
    BlobNotFoundError,
    BlobStore,
    EncryptedBlobStore,
    InMemoryBlobStore,
    StorageError,
    new_storage_key,
)

__all__ = [
    "EICAR",
    "MAX_ANY_BYTES",
    "MAX_BYTES",
    "SIGNATURES",
    "UPLOAD_DOCUMENT",
    "VIEW_DOCUMENT",
    "BlobNotFoundError",
    "BlobStore",
    "ContentTypeError",
    "Document",
    "DocumentContentType",
    "DocumentError",
    "DocumentInfected",
    "DocumentNotFound",
    "DocumentNotReleasable",
    "DocumentPostingLink",
    "DocumentRepository",
    "DocumentService",
    "DocumentStatus",
    "DocumentTooLarge",
    "EncryptedBlobStore",
    "InMemoryBlobStore",
    "LocalPatternScanner",
    "MalwareScanner",
    "NotAuthorizedForDocument",
    "RefusingScanner",
    "RetentionBasis",
    "ScanResult",
    "ScanStatus",
    "ScanUnavailable",
    "SqlDocumentRepository",
    "StorageError",
    "UnsupportedPosting",
    "add_years",
    "build_scanner",
    "expired_documents",
    "is_expired",
    "new_storage_key",
    "retention_until",
    "sniff",
    "verify_declared",
]
