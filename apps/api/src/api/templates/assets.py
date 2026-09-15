"""Uploading, storing and resolving FR-TPL-001's logo assets - FR-TPL-018,
SEC-005.

--- The pipeline, and why the order is the security control ---

Exactly `api.documents.service`'s shape, deliberately - a logo is a file
upload like any other for SEC-005's purposes, and there is no reason this
pipeline should look any different from that one:

    authorize -> cap size -> sniff type -> (SVG only) sanitize -> scan ->
    store (encrypted) -> record -> audit

The size cap precedes the sniff so an absurd body is refused unexamined. The
SVG sanitizer runs BEFORE the malware scan and BEFORE storage: the bytes that
get scanned and stored are the SANITIZED bytes, never the original upload -
running the scanner on unsanitized bytes and then storing the sanitized
result would mean the thing that was actually judged is not the thing that
was actually kept. The row is written last, so a failure leaves an orphan
blob (collected by a sweep, exactly as `api.documents.storage`'s module
docstring describes) rather than a row pointing at nothing.

--- Not `api.documents` ---

Reuses `api.documents.storage` (`BlobStore`/`EncryptedBlobStore`/
`new_storage_key`) and `api.documents.scanning` (`MalwareScanner`/
`build_scanner`) directly - both are already tenant/administration-scoped and
provider-agnostic, and duplicating either would be pointless. Does NOT reuse
`api.documents.content_type` or `api.documents.service`: those modules exist
to refuse SVG outright, and `template_asset` (migration 0042) is a
deliberately different, narrower table from `document` (0031) precisely
because a logo has none of a source document's retention or immutability
obligations - see 0042's own header comment.

--- Why an infected or unscannable upload is refused, not quarantined ---

`document.scan_status` (0031) is a state machine because a source document is
evidence that must be kept (even infected - it's evidence about an incident)
and FR-DOC-005 forbids deleting it early. `template_asset` (0042) carries no
such column at all: a logo is not evidence and has no retention obligation, so
there is nothing to gain by storing an infected or unscanned file under
quarantine. A non-clean scan result here means nothing is stored at all - the
upload simply fails, the same way a malformed PNG fails `load_image`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.crypto.envelope import EnvelopeEncryptionService
from api.crypto.kms import build_kms
from api.crypto.repository import SqlAdministrationKeyRepository
from api.documents.scanning import MalwareScanner, ScanStatus
from api.documents.storage import BlobStore, EncryptedBlobStore, build_blob_store, new_storage_key
from api.invoicing.model import NotAuthorizedToInvoice
from api.invoicing.pdf import Image, ImageError, load_image
from api.invoicing.service import CREATE_INVOICE
from api.templates.asset_content_type import (
    TemplateAssetContentType,
    TemplateAssetContentTypeError,
    sniff,
)
from api.templates.svg_sanitizer import SvgSanitizationError, sanitize_svg

__all__ = [
    "MAX_UPLOAD_BYTES",
    "TemplateAsset",
    "TemplateAssetsError",
    "TemplateAssetNotFound",
    "TemplateAssetTooLarge",
    "TemplateAssetInvalidType",
    "TemplateAssetInvalidSvg",
    "TemplateAssetInfected",
    "TemplateAssetScanUnavailable",
    "LogoNotRenderable",
    "TemplateAssetRepository",
    "TemplateAssetService",
    "build_administration_blob_store",
    "build_resolve_logo",
]

#: Raw-body cap, checked BEFORE sniffing - mirrors `api.documents.service`'s
#: order exactly, for the same reason: a body far too large to be any
#: accepted asset is refused without being examined at all. 5 MiB is generous
#: for a PNG/JPEG logo (the larger of the three accepted formats in practice;
#: `api.templates.svg_sanitizer.MAX_SVG_BYTES` caps SVG more tightly, at 2 MiB,
#: inside `sanitize_svg` itself) while bounding the worst case.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

#: Process-wide blob store, selected by settings.blob_provider (ADR-062) -
#: the same `api.documents.storage.build_blob_store` factory
#: `api.documents.routes` calls for its own. Deliberately a SEPARATE call
#: (not an import of that module's instance): template assets and documents
#: are different bounded stores (0042's `template_asset` is not `document`,
#: per that migration's own header comment). In "in-memory" mode this
#: naturally yields two independent dicts, since each call to
#: build_blob_store() constructs a fresh InMemoryBlobStore; in "r2" mode
#: both point at the same bucket, distinguished only by their own
#: `new_storage_key`-derived prefixes - the two tables' isolation from each
#: other was never about which Python object or bucket held the bytes.
_local_blobs = build_blob_store()


@dataclass(frozen=True, slots=True)
class TemplateAsset:
    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    storage_key: str
    content_type: TemplateAssetContentType
    #: True once the stored bytes have actually been through
    #: `sanitize_svg` (for SVG) or content-type verification (for PNG/JPEG).
    #: Mirrors migration 0042's `template_asset.sanitized` column exactly.
    sanitized: bool
    created_at: datetime | None = None


class TemplateAssetsError(Exception):
    """Base for this module's refusals."""


class TemplateAssetNotFound(TemplateAssetsError):
    """No such asset in this administration - indistinguishable from
    "belongs to another tenant", the same posture every other *NotFound in
    this codebase takes for the same RLS reason.
    """


class TemplateAssetTooLarge(TemplateAssetsError):
    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"{size} bytes exceeds the {limit}-byte logo upload limit")


class TemplateAssetInvalidType(TemplateAssetsError):
    """`sniff()` refused the bytes - not one of PNG/JPEG/SVG by content."""


class TemplateAssetInvalidSvg(TemplateAssetsError):
    """`sanitize_svg` refused the file. Carries the sanitizer's own D5-shaped
    reason verbatim, since it is already specific and actionable.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class TemplateAssetInfected(TemplateAssetsError):
    """The malware scanner reported a detection. Nothing was stored."""


class TemplateAssetScanUnavailable(TemplateAssetsError):
    """The scanner could not answer. Refused rather than stored unscanned
    (SEC-005) - nothing here to quarantine, unlike `document`, since a
    non-clean result means nothing is ever written."""


class LogoNotRenderable(TemplateAssetsError):
    """FR-TPL-001/018: this template's logo cannot be embedded in a rendered
    PDF today.

    The one case that actually reaches this today is an SVG logo:
    `api.invoicing.pdf` has no vector-drawing capability at all (see that
    module's docstring on images - "SVG is not here at all"), so an SVG
    cannot be embedded in the real issued PDF or in the FR-TPL-008 preview,
    which renders through the exact same engine. Raised by the `resolve_logo`
    hook `build_resolve_logo` returns, and caught at both call sites that
    actually render a document from a template: invoice issue
    (`api.invoicing.routes.issue_invoice`) and template preview
    (`api.templates.routes.preview_template`) - never silently swallowed into
    "no logo drawn", which would misrepresent a real, user-visible gap as
    success.
    """


class TemplateAssetRepository(Protocol):
    async def record(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        storage_key: str,
        content_type: str,
        sanitized: bool,
    ) -> TemplateAsset: ...

    async def get(
        self, *, administration_id: uuid.UUID, asset_id: uuid.UUID
    ) -> TemplateAsset | None: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...


class TemplateAssetService:
    """FR-TPL-001's upload/download pair. One instance per request, already
    scoped to an administration by the blob store it is handed - the same
    shape `api.documents.service.DocumentService` takes.
    """

    def __init__(
        self,
        repository: TemplateAssetRepository,
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

    async def upload(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        data: bytes,
        declared_content_type: str | None = None,
        correlation_id: str | None = None,
    ) -> TemplateAsset:
        await self._require(actor_user_id, administration_id)

        # Before anything reads the bytes further - see the module docstring.
        if len(data) > MAX_UPLOAD_BYTES:
            raise TemplateAssetTooLarge(len(data), MAX_UPLOAD_BYTES)

        try:
            content_type = sniff(data)
        except TemplateAssetContentTypeError as exc:
            raise TemplateAssetInvalidType(str(exc)) from exc

        if content_type is TemplateAssetContentType.SVG:
            try:
                payload = sanitize_svg(data)
            except SvgSanitizationError as exc:
                await self._record_event(
                    administration_id=administration_id,
                    user_id=actor_user_id,
                    action="reject_unsafe_svg_logo",
                    outcome=AuditOutcome.DENIED,
                    correlation_id=correlation_id,
                    detail={"reason": str(exc)},
                )
                raise TemplateAssetInvalidSvg(str(exc)) from exc
        else:
            # PNG/JPEG: verified by content above; there is no further
            # sanitisation step for a raster format, so "sanitized" describes
            # "went through content-type verification and malware scanning"
            # for these two - see 0042's own comment on the column.
            payload = data

        scan = await self._scanner.scan(payload)
        if scan.status is ScanStatus.INFECTED:
            await self._record_event(
                administration_id=administration_id,
                user_id=actor_user_id,
                action="reject_infected_template_asset",
                outcome=AuditOutcome.DENIED,
                correlation_id=correlation_id,
                detail={
                    "scanner": scan.scanner,
                    "threat": scan.detail,
                    "content_type": content_type.value,
                },
            )
            raise TemplateAssetInfected(
                f"{scan.scanner} reported a detection; the upload was refused and "
                f"nothing was stored"
            )
        if scan.status is not ScanStatus.CLEAN:
            raise TemplateAssetScanUnavailable(
                f"{scan.scanner} could not scan this file ({scan.detail}). The "
                f"upload is refused rather than stored unscanned (SEC-005)."
            )

        storage_key = new_storage_key(administration_id)
        await self._blobs.put(storage_key, payload)

        organization_id = await self._organization_of(administration_id)
        asset = await self._repository.record(
            organization_id=organization_id,
            administration_id=administration_id,
            storage_key=storage_key,
            content_type=content_type.value,
            sanitized=True,
        )
        await self._record_event(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="upload_template_asset",
            correlation_id=correlation_id,
            detail={
                "asset_id": str(asset.id),
                "content_type": asset.content_type.value,
                "byte_size": len(payload),
                "scanner": scan.scanner,
            },
        )
        return asset

    async def download(
        self, *, administration_id: uuid.UUID, asset_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> tuple[TemplateAsset, bytes]:
        await self._require(actor_user_id, administration_id)
        asset = await self._repository.get(administration_id=administration_id, asset_id=asset_id)
        if asset is None:
            raise TemplateAssetNotFound(f"template asset {asset_id} not found")
        data = await self._blobs.get(asset.storage_key)
        return asset, data

    # -- internals -----------------------------------------------------------

    async def _organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise TemplateAssetNotFound(f"administration {administration_id} does not exist")
        return organization_id

    async def _require(self, user_id: uuid.UUID, administration_id: uuid.UUID) -> None:
        """The IDENTICAL `create sales_invoice` permission every other write
        in the template designer checks - see `api.templates.service`'s
        docstring (ADR-041, ADR-012): a logo is part of what "shaping what a
        sales invoice looks like" already means, not a capability of its own.
        """
        action, resource_type = CREATE_INVOICE
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
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise NotAuthorizedToInvoice(action, resource_type, decision.detail or decision.reason)

    async def _record_event(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        detail: dict[str, object] | None = None,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        correlation_id: str | None = None,
    ) -> None:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            return
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="template_asset",
                resource_id=administration_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=dict(detail or {}),
            )
        )


# =============================================================================
# Wiring shared by both routes that construct a request-scoped store, and by
# the rendering hook below
# =============================================================================


def build_administration_blob_store(
    *, administration_id: uuid.UUID, session: AsyncSession
) -> EncryptedBlobStore:
    """The exact same construction `api.documents.routes.get_document_service`
    uses for its own blob store - `EnvelopeEncryptionService` built the same
    way, wrapping the same kind of per-administration key. Kept as one
    function so the two upload pipelines (documents, template assets) cannot
    silently diverge in how they derive an administration's encryption key.
    """
    encryption = EnvelopeEncryptionService(build_kms(), SqlAdministrationKeyRepository(session))
    return EncryptedBlobStore(_local_blobs, encryption, administration_id)


async def resolve_logo_image(
    *,
    repository: TemplateAssetRepository,
    blobs: BlobStore,
    administration_id: uuid.UUID,
    asset_id: uuid.UUID,
) -> Image:
    """The actual PNG/JPEG resolution `TemplatedPdfRenderer.render` needs, or
    a `LogoNotRenderable` refusal for anything it cannot embed.

    No permission check of its own: this is called only from INSIDE an
    already-authorized render (`SalesPostingService._store_rendering` at
    issue, `InvoiceTemplateService.preview` at preview time), both of which
    sit behind their own route-level `require_permission("create",
    "sales_invoice", ...)` check before rendering ever starts - the same
    "already scoped, not re-checked" posture `TemplatedPdfRenderer` itself
    takes toward the `template` it is handed.
    """
    asset = await repository.get(administration_id=administration_id, asset_id=asset_id)
    if asset is None:
        # Guarded against by 0042's `invoice_template_logo_same_tenant_trg`
        # under ordinary operation - a template's logo_asset_id can only ever
        # be set to an asset in the same administration - so this branch is a
        # defensive "the row disappeared out from under us" case, not a
        # reachable user error.
        raise LogoNotRenderable(
            f"this template's logo (asset {asset_id}) could not be found; remove "
            f"it and upload a new logo."
        )
    if asset.content_type is TemplateAssetContentType.SVG:
        raise LogoNotRenderable(
            "this template's logo is an SVG, which cannot yet be embedded in a "
            "rendered PDF; upload a PNG or JPEG version to include a logo on "
            "issued invoices."
        )
    data = await blobs.get(asset.storage_key)
    try:
        return load_image(data)
    except ImageError as exc:
        raise LogoNotRenderable(
            f"this template's logo could not be embedded in the rendered PDF "
            f"({exc}); upload it again in a supported format."
        ) from exc


def build_resolve_logo(
    *, administration_id: uuid.UUID, session: AsyncSession
) -> Callable[[object], Awaitable[Image | None]]:
    """The `resolve_logo` hook `api.invoicing.rendering.TemplatedPdfRenderer`
    accepts, built once per request and shared by both call sites that
    construct a renderer: `api.invoicing.routes.get_invoicing_service` (issue)
    and `api.templates.routes.get_template_service` (preview) - see
    `api.invoicing.rendering`'s module docstring on why "the same engine"
    means literally the same class either way.

    Typed loosely (`Callable[[object], ...]`, `template` read via
    `template.logo.asset_id` rather than importing `InvoiceTemplate` for a
    precise signature) for the identical reason `TemplatedPdfRenderer.
    __init__` itself accepts `resolve_logo: object` - see that class's
    docstring.
    """
    # Deferred import: `api.templates.asset_repository` is the SQL layer, and
    # keeping this import inside the function (rather than at module scope)
    # means `api.templates.assets` - imported by `api.invoicing.routes`, a
    # different package - never pulls SQLAlchemy's repository layer in until a
    # request actually needs it.
    from api.templates.asset_repository import SqlTemplateAssetRepository

    repository = SqlTemplateAssetRepository(session)
    blobs = build_administration_blob_store(administration_id=administration_id, session=session)

    async def resolve_logo(template: object) -> Image | None:
        asset_id = template.logo.asset_id  # type: ignore[attr-defined]
        if asset_id is None:
            return None
        return await resolve_logo_image(
            repository=repository,
            blobs=blobs,
            administration_id=administration_id,
            asset_id=asset_id,
        )

    return resolve_logo
