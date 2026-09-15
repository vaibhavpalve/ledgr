"""The document archive's HTTP surface - FR-DOC-001..004, SEC-005.

Registered on the app in `api.main`. A separate module rather than more of
main.py because the download route carries response headers that are security
controls in their own right, and they need the room to say why.

--- SEC-005's last two clauses live here ---

    SEC-005  ... stored outside the web root, served from a SEPARATE ORIGIN
             with `Content-Disposition: attachment`.

Both are about what a browser does with bytes it did not write:

  * `Content-Disposition: attachment` makes the browser save the file instead
    of rendering it. A PDF rendered inline runs its own JavaScript in the
    origin that served it; an HTML file mis-stored as an image renders as a
    page. `attachment` removes the rendering step entirely.
  * A separate origin means that even if something renders, it renders
    somewhere with no session cookie, no same-origin access to the app, and
    nothing worth stealing. Defence in depth, because the first control is one
    header away from being forgotten.

The separate origin is a deployment fact, so this module ENFORCES it rather
than assuming it: `settings.document_origin` has to be configured and has to
differ from the API's own origin, and the download route refuses to serve
otherwise. A control that silently degrades to "same origin" in an environment
somebody set up quickly is not a control.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.authz.dependencies import (
    administration_from_path,
    get_authorization_service,
    require_permission,
)
from api.authz.model import AuthorizationDecision
from api.authz.service import AuthorizationService
from api.config import settings
from api.crypto.envelope import EnvelopeEncryptionService
from api.crypto.kms import build_kms
from api.crypto.repository import SqlAdministrationKeyRepository
from api.db import get_db_session
from api.documents.content_type import ContentTypeError
from api.documents.model import (
    MAX_ANY_BYTES,
    DocumentInfected,
    DocumentNotFound,
    DocumentNotReleasable,
    DocumentTooLarge,
    ScanUnavailable,
)
from api.documents.repository import SqlDocumentRepository
from api.documents.retention import RetentionBasis
from api.documents.scanning import build_scanner
from api.documents.service import DocumentService
from api.documents.storage import EncryptedBlobStore, build_blob_store
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context

#: Process-wide blob store, selected by settings.blob_provider (ADR-062) -
#: "in-memory" for local dev/tests, "r2" in production. Built once per
#: process, like api.mail.outbox's collecting sender, rather than per
#: request - a fresh R2BlobStore per request would open a new client for no
#: benefit, since R2BlobStore itself opens a connection per call already.
_blobs = build_blob_store()


def register(app: FastAPI) -> None:
    """Adds these routes DIRECTLY to the app, rather than via include_router().

    That is not a style preference, and getting it wrong is silent. This
    FastAPI version does not flatten an included router's routes into
    `app.routes`: it appends one opaque `fastapi.routing._IncludedRouter`
    wrapper instead. Everything this codebase relies on to make a route safe
    walks `app.routes` and reads each entry's `dependant`:

        api.authz_middleware   refuses to dispatch to a route that declares no
                               permission requirement - and cannot find the
                               declaration through the wrapper
        tests/test_authz_coverage.py     CLAUDE.md rule three, at build time
        tests/test_audit_coverage.py     IAM-090
        tests/test_isolation_coverage.py IAM-005
        tests/test_idempotency_coverage.py NFR-032

    Routes hidden inside a wrapper are invisible to all five. The coverage
    checks would pass by never seeing them, which is the worst possible
    outcome: three endpoints that touch financial evidence, exempt from every
    gate, and green.

    `add_api_route` builds an ordinary APIRoute on the app, with the same
    dependency tree the decorator form produces, so the declarations in each
    handler's signature are found exactly as they are for every other route.
    """
    app.add_api_route(
        "/v1/administrations/{administration_id}/documents",
        upload_document,
        methods=["POST"],
        name="upload_document",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/documents/{document_id}",
        get_document,
        methods=["GET"],
        name="get_document",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/documents/{document_id}/content",
        download_document,
        methods=["GET"],
        name="download_document",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/documents/{document_id}/erasure-request",
        request_document_erasure,
        methods=["POST"],
        name="request_document_erasure",
    )


async def get_document_service(
    administration_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> DocumentService:
    """Built per request and already scoped to one administration.

    The blob store is constructed around THIS administration's key, so a
    handler cannot encrypt or decrypt against another tenant's - the id is not
    a parameter it could get wrong. AES-GCM binds the same pair into its
    associated data, so a mistake here fails to decrypt rather than decrypting
    something else (see api.documents.storage._unpack).
    """
    encryption = EnvelopeEncryptionService(build_kms(), SqlAdministrationKeyRepository(session))
    return DocumentService(
        repository=SqlDocumentRepository(session),
        blobs=EncryptedBlobStore(_blobs, encryption, administration_id),
        scanner=build_scanner(settings.malware_scanner_provider),
        authorization=authorization,
        audit_log=AuditLog(SqlAuditRepository(session)),
    )


def _document_json(document: object) -> dict[str, object]:
    """One shape, produced once, so web and mobile cannot disagree.

    `storage_key` is deliberately absent: it is an internal reference to a
    blob, and a client that had it would be a client tempted to fetch the blob
    directly - which is the path that has no scan gate, no audit entry and none
    of SEC-005's response headers.
    """
    return {
        "id": str(document.id),  # type: ignore[attr-defined]
        "content_type": document.content_type.value,  # type: ignore[attr-defined]
        "byte_size": document.byte_size,  # type: ignore[attr-defined]
        "original_filename": document.original_filename,  # type: ignore[attr-defined]
        "content_sha256": document.content_hash.hex(),  # type: ignore[attr-defined]
        "retention_until": document.retention_until.isoformat(),  # type: ignore[attr-defined]
        "retention_basis": document.retention_basis.value,  # type: ignore[attr-defined]
        "status": document.status.value,  # type: ignore[attr-defined]
        "scan_status": document.scan_status.value,  # type: ignore[attr-defined]
        "uploaded_at": document.uploaded_at.isoformat()  # type: ignore[attr-defined]
        if document.uploaded_at  # type: ignore[attr-defined]
        else None,
    }


async def upload_document(
    administration_id: uuid.UUID,
    request: Request,
    fiscal_year_id: uuid.UUID,
    filename: str | None = None,
    immovable_property: bool = False,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DocumentService = Depends(get_document_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "upload",
            "document",
            scope=administration_from_path("administration_id"),
            # IAM-090: a source document arriving is the evidence a posting
            # will rest on, so its arrival is a thing an auditor asks about.
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-DOC-001 and SEC-005.

    `fiscal_year_id` is required rather than derived from today's date: FR-DOC-002
    anchors retention to the fiscal year the document BELONGS to, and a receipt
    uploaded in March for last year is the ordinary case, not the exception.

    `immovable_property` is asked rather than inferred. Whether a document
    relates to immovable property is an accounting judgement about the
    transaction behind it - it is why the Dutch revision period runs to ten
    years - and nothing in a file's bytes settles it. A system that guessed
    would guess short.

    --- The body is the file, not a multipart part ---

    The bytes arrive as the raw request body with `Content-Type` describing
    them, the way an object store's PUT works, rather than as a multipart form.
    That is a security choice before it is an ergonomic one: multipart needs a
    parser, and a parser is code that runs over attacker-controlled bytes
    before anything has decided whether to accept them. SEC-006 isolates the
    parsing LEDGR cannot avoid; this avoids one it can.

    It costs nothing at the client. A `File` is a `Blob`, so a file picker or a
    drag-and-drop (FR-EXP-001) sends it directly:

        fetch(url, {method: 'POST', body: file,
                    headers: {'Content-Type': file.type}})

    and FR-EXP-001a's batch capture is several requests rather than one, which
    is what its "each becoming a separate expense" wants anyway - and gives
    each its own idempotency key (NFR-032).
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    # Refused from the declared length BEFORE the body is read, so an oversized
    # upload costs a header rather than however many gigabytes it claimed. The
    # value is untrusted - a client can lie or omit it - which is why the real
    # check below still runs on the bytes that actually arrived.
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > MAX_ANY_BYTES:
        raise problem(
            request,
            413,
            "errors.document_too_large",
            reason="document_too_large",
            limit_bytes=MAX_ANY_BYTES,
        )

    # Read once, in full. Every later step - sniffing, hashing, scanning - must
    # see the SAME bytes, and a stream re-read between them is the
    # time-of-check-to-time-of-use gap SEC-005 exists to close.
    data = await request.body()
    if len(data) > MAX_ANY_BYTES:
        raise problem(
            request,
            413,
            "errors.document_too_large",
            reason="document_too_large",
            limit_bytes=MAX_ANY_BYTES,
        )

    try:
        document = await service.upload(
            administration_id=administration_id,
            fiscal_year_id=fiscal_year_id,
            actor_user_id=tenant.user_id,
            data=data,
            original_filename=filename,
            declared_content_type=request.headers.get("content-type"),
            retention_basis=RetentionBasis.IMMOVABLE_PROPERTY
            if immovable_property
            else RetentionBasis.STANDARD,
        )
    except DocumentTooLarge as exc:
        raise problem(
            request,
            413,
            "errors.document_too_large",
            reason="document_too_large",
            limit_bytes=exc.limit,
        ) from exc
    except ContentTypeError as exc:
        raise problem(
            request, 415, "errors.document_type_rejected", reason="unsupported_document_type"
        ) from exc
    except DocumentInfected as exc:
        # 422 rather than 400: the request was well-formed and the FILE was
        # refused. The `reason` is what a client branches on to say so.
        raise problem(request, 422, "errors.document_infected", reason="document_infected") from exc
    except ScanUnavailable as exc:
        raise problem(
            request, 503, "errors.document_scan_unavailable", reason="scan_unavailable"
        ) from exc

    return _document_json(document)


async def get_document(
    administration_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DocumentService = Depends(get_document_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "document",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """Metadata only. The bytes are a separate call, on a separate origin."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        document = await service.metadata(
            administration_id=administration_id,
            document_id=document_id,
            actor_user_id=tenant.user_id,
        )
    except DocumentNotFound as exc:
        raise problem(
            request, 404, "errors.document_not_found", reason="document_not_found"
        ) from exc
    return _document_json(document)


async def download_document(
    administration_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DocumentService = Depends(get_document_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "document",
            scope=administration_from_path("administration_id"),
            # IAM-090's "data reads of financial records". Taking a copy of the
            # evidence a posting rests on is the read an auditor asks about.
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> Response:
    """The original bytes, unaltered - FR-DOC-001, SEC-005.

    Every header below is a control rather than a convenience:

      Content-Disposition: attachment
          The browser saves rather than renders. A PDF rendered inline runs its
          own scripts in the origin that served it.
      X-Content-Type-Options: nosniff
          Stops a browser second-guessing the type we verified from the bytes
          and rendering it as something else - which would undo SEC-005's first
          clause at the last possible moment.
      Content-Security-Policy: sandbox; default-src 'none'
          For anything that renders anyway. Nothing loads, nothing executes.
      Cache-Control: private, no-store
          A source document is financial evidence under a 7-year retention
          policy; it does not belong in a shared cache, and `no-store` keeps it
          off disk on the way through.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    verify_separate_origin_configured()

    try:
        document, data = await service.original(
            administration_id=administration_id,
            document_id=document_id,
            actor_user_id=tenant.user_id,
        )
    except DocumentNotFound as exc:
        raise problem(
            request, 404, "errors.document_not_found", reason="document_not_found"
        ) from exc
    except DocumentNotReleasable as exc:
        # 409 rather than 403: the caller is permitted, the FILE is not ready.
        # `pending`, `failed` and `infected` all land here with the same
        # answer - an unscanned file and an infected one are the same risk to
        # whoever opens them, and the difference is only time.
        raise problem(
            request,
            409,
            "errors.document_not_releasable",
            reason="document_not_releasable",
            scan_status=exc.scan_status.value,
        ) from exc

    return Response(
        content=data,
        media_type=document.content_type.value,
        headers={
            # The filename is quoted and the fallback is deliberate: a
            # download must not depend on a client parsing a name that came
            # from an upload. RFC 6266's `filename*` is not used because the
            # stored name is untrusted input and this is not the place to
            # start decoding it.
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, no-store",
            # Not a security control - a convenience for a client verifying it
            # got what the archive holds, and the same value the metadata
            # endpoint returns.
            "X-Content-SHA256": document.content_hash.hex(),
        },
    )


async def request_document_erasure(
    administration_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DocumentService = Depends(get_document_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Reused rather than invented (ADR-012): `link_to_posting` already
            # gates a mutating, evidentiary act behind this permission, and an
            # erasure request is at least as privileged.
            "upload",
            "document",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """PRIV-022/PRIV-023.

    Always 200: the request is always ACTED on, whichever way it resolves.
    `outcome` in the body is `erased` or `restricted`, and `explanation` names
    the rule and the date - the "clear, specific explanation" PRIV-022 asks
    for, not a generic refusal.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        decision = await service.request_erasure(
            administration_id=administration_id,
            document_id=document_id,
            actor_user_id=tenant.user_id,
        )
    except DocumentNotFound as exc:
        raise problem(
            request, 404, "errors.document_not_found", reason="document_not_found"
        ) from exc
    return decision.as_dict()


def verify_separate_origin_configured() -> None:
    """SEC-005's "served from a separate origin", enforced rather than assumed.

    Checks the CONFIGURATION rather than the request's own hostname, and that
    is deliberate. Comparing `request.url.hostname` looks stricter and is
    weaker: behind a reverse proxy it reflects whatever the proxy passed
    through, so the check would pass or fail for reasons unrelated to how the
    deployment is actually set up, and would need a trusted-proxy story before
    it meant anything (the caveat `api.rate_limit_middleware` already carries
    about client IPs).

    What can be checked exactly is that somebody made the decision: a document
    origin exists, and it is not the API's. A deployment that skipped it would
    otherwise serve documents from the origin holding the session cookie while
    passing every header-level test, because those tests check the header and
    not the host.
    """
    if not settings.document_origin:
        raise RuntimeError(
            "DOCUMENT_ORIGIN is not configured. SEC-005 requires documents to be "
            "served from a separate origin, and there is deliberately no default: "
            "serving them from the API's own origin would put untrusted bytes "
            "where the session cookie is."
        )
    if _hostname_of(settings.document_origin) == _hostname_of(settings.api_origin):
        raise RuntimeError(
            f"DOCUMENT_ORIGIN ({settings.document_origin}) and API_ORIGIN "
            f"({settings.api_origin}) resolve to the same host. SEC-005's separate "
            f"origin is what keeps a rendered document away from the session "
            f"cookie; one origin serving both is not it."
        )


def _hostname_of(origin: str) -> str:
    return urlparse(origin).hostname or origin
