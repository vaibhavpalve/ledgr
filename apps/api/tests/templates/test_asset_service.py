"""FR-TPL-001's upload pipeline - FR-TPL-018, SEC-005.

Mirrors `tests/documents/test_service.py`'s posture: most of these tests are
about what has NOT happened when a step refuses - nothing stored, no row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from api.audit.log import AuditLog
from api.authz.model import AuthorizationDecision
from api.documents.scanning import EICAR, LocalPatternScanner, RefusingScanner
from api.documents.storage import InMemoryBlobStore
from api.templates.asset_content_type import TemplateAssetContentType, TemplateAssetContentTypeError
from api.templates.assets import (
    LogoNotRenderable,
    TemplateAsset,
    TemplateAssetInfected,
    TemplateAssetInvalidSvg,
    TemplateAssetInvalidType,
    TemplateAssetNotFound,
    TemplateAssetScanUnavailable,
    TemplateAssetService,
    TemplateAssetTooLarge,
    resolve_logo_image,
)
from api.templates.svg_sanitizer import sanitize_svg
from tests.support.fake_audit_repository import InMemoryAuditRepository

pytestmark = pytest.mark.anyio

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
#: A genuinely decodable 1x1 PNG - unlike the placeholder `PNG` bytes above
#: (which are only ever used where nothing needs to actually LOAD the image),
#: `resolve_logo_image`'s tests need `api.invoicing.pdf.load_image` to
#: succeed on this one.
REAL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000"
    "907753de0000000c4944415478da63f8cfc0000003010100f703414300"
    "00000049454e44ae426082"
)
CLEAN_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0 L1 1"/></svg>'
#: Refused outright by `sanitize_svg` (a DOCTYPE, never stripped-and-
#: continued) - see `api.templates.svg_sanitizer`'s own test suite for the
#: full adversarial matrix; this module only needs one representative case to
#: prove the upload PIPELINE refuses on it.
UNSANITIZABLE_SVG = b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>'
#: Not refused - STRIPPED. `<script>` is not on the sanitiser's element
#: allowlist, so it is silently dropped and the upload succeeds with clean
#: bytes; see `test_a_hostile_svg_is_sanitized_rather_than_refused` below.
SCRIPT_BEARING_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

ORG = uuid.uuid4()
ADMIN = uuid.uuid4()
ACTOR = uuid.uuid4()


@dataclass
class FakeAssetRepository:
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    assets: dict[uuid.UUID, TemplateAsset] = field(default_factory=dict)

    async def record(
        self, *, organization_id, administration_id, storage_key, content_type, sanitized
    ):  # type: ignore[no-untyped-def]
        asset = TemplateAsset(
            id=uuid.uuid4(),
            organization_id=organization_id,
            administration_id=administration_id,
            storage_key=storage_key,
            content_type=TemplateAssetContentType(content_type),
            sanitized=sanitized,
        )
        self.assets[asset.id] = asset
        return asset

    async def get(self, *, administration_id, asset_id):  # type: ignore[no-untyped-def]
        asset = self.assets.get(asset_id)
        if asset is None or asset.administration_id != administration_id:
            return None
        return asset

    async def organization_of(self, *, administration_id):  # type: ignore[no-untyped-def]
        return self.organization_id if administration_id == self.administration_id else None


class AllowAllAuthorization:
    async def authorize(self, request: object) -> AuthorizationDecision:
        return AuthorizationDecision(allowed=True, reason="allowed")


class DenyAllAuthorization:
    async def authorize(self, request: object) -> AuthorizationDecision:
        return AuthorizationDecision(allowed=False, reason="no_matching_grant")


def _service(
    *, scanner=None, authorization=None, repository: FakeAssetRepository | None = None
) -> tuple[TemplateAssetService, FakeAssetRepository, InMemoryBlobStore]:
    repo = repository or FakeAssetRepository(organization_id=ORG, administration_id=ADMIN)
    blobs = InMemoryBlobStore()
    service = TemplateAssetService(
        repository=repo,  # type: ignore[arg-type]
        blobs=blobs,
        scanner=scanner or LocalPatternScanner(),
        authorization=authorization or AllowAllAuthorization(),  # type: ignore[arg-type]
        audit_log=AuditLog(InMemoryAuditRepository()),
    )
    return service, repo, blobs


async def test_a_clean_png_is_stored_and_recorded_as_sanitized() -> None:
    service, repo, blobs = _service()
    asset = await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=PNG)

    assert asset.content_type is TemplateAssetContentType.PNG
    assert asset.sanitized is True
    assert len(blobs) == 1
    assert repo.assets[asset.id] is asset


async def test_a_clean_svg_is_sanitized_before_being_stored() -> None:
    service, repo, blobs = _service()
    asset = await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=CLEAN_SVG)

    assert asset.content_type is TemplateAssetContentType.SVG
    assert asset.sanitized is True
    stored = await blobs.get(asset.storage_key)
    # The row must never claim `sanitized = true` for an SVG that did not
    # actually go through `sanitize_svg` - proven here by checking the STORED
    # bytes are the sanitizer's own output, not the raw upload.
    assert stored == sanitize_svg(CLEAN_SVG)


async def test_a_hostile_svg_is_sanitized_rather_than_refused() -> None:
    """FR-TPL-018 strips scripts; it does not refuse a file merely for
    containing one. The upload SUCCEEDS, and what is actually stored has no
    trace of the script - the important assertion is on the bytes, not just
    the absence of an exception.
    """
    service, repo, blobs = _service()
    asset = await service.upload(
        administration_id=ADMIN, actor_user_id=ACTOR, data=SCRIPT_BEARING_SVG
    )

    stored = await blobs.get(asset.storage_key)
    assert b"script" not in stored
    assert b"alert" not in stored


async def test_an_unsanitizable_svg_is_refused_and_nothing_is_stored() -> None:
    """Unlike a script (stripped), a DOCTYPE is refused OUTRIGHT by
    `sanitize_svg` - the upload pipeline must surface that as a refusal, not
    silently continue with whatever `sanitize_svg` would otherwise produce.
    """
    service, repo, blobs = _service()
    with pytest.raises(TemplateAssetInvalidSvg):
        await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=UNSANITIZABLE_SVG)

    assert len(blobs) == 0
    assert repo.assets == {}


async def test_an_unrecognised_type_is_refused_before_scanning_or_storage() -> None:
    service, repo, blobs = _service()
    with pytest.raises(TemplateAssetInvalidType) as excinfo:
        await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=b"not an image")

    # Wraps the sniffer's own, more specific refusal rather than swallowing it.
    assert isinstance(excinfo.value.__cause__, TemplateAssetContentTypeError)
    assert len(blobs) == 0


async def test_an_oversized_upload_is_refused_before_it_is_examined() -> None:
    from api.templates import assets as assets_module

    service, repo, blobs = _service()
    oversized = b"\x89PNG\r\n\x1a\n" + b"0" * (assets_module.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(TemplateAssetTooLarge):
        await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=oversized)

    assert len(blobs) == 0


async def test_an_infected_upload_is_refused_and_nothing_is_stored() -> None:
    service, repo, blobs = _service(scanner=LocalPatternScanner())
    infected = PNG + EICAR
    with pytest.raises(TemplateAssetInfected):
        await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=infected)

    assert len(blobs) == 0
    assert repo.assets == {}


async def test_a_failed_scan_is_refused_rather_than_stored_unscanned() -> None:
    service, repo, blobs = _service(scanner=RefusingScanner())
    with pytest.raises(TemplateAssetScanUnavailable):
        await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=PNG)

    assert len(blobs) == 0


async def test_upload_is_refused_without_the_shared_permission() -> None:
    service, repo, blobs = _service(authorization=DenyAllAuthorization())
    from api.invoicing.model import NotAuthorizedToInvoice

    with pytest.raises(NotAuthorizedToInvoice):
        await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=PNG)

    assert len(blobs) == 0


async def test_download_returns_the_stored_bytes() -> None:
    service, repo, blobs = _service()
    uploaded = await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=PNG)

    asset, data = await service.download(
        administration_id=ADMIN, asset_id=uploaded.id, actor_user_id=ACTOR
    )
    assert asset.id == uploaded.id
    assert data == PNG


async def test_downloading_an_unknown_asset_is_refused() -> None:
    service, repo, blobs = _service()
    with pytest.raises(TemplateAssetNotFound):
        await service.download(administration_id=ADMIN, asset_id=uuid.uuid4(), actor_user_id=ACTOR)


class TestResolveLogoImage:
    """`api.templates.assets.resolve_logo_image` - the hook that closes the
    `resolve_logo` gap in `TemplatedPdfRenderer` for PNG/JPEG, and refuses
    cleanly (never silently) for SVG.
    """

    async def test_a_png_logo_resolves_to_a_placeable_image(self) -> None:
        service, repo, blobs = _service()
        asset = await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=REAL_PNG)

        image = await resolve_logo_image(
            repository=repo, blobs=blobs, administration_id=ADMIN, asset_id=asset.id
        )
        assert image.width > 0 and image.height > 0

    async def test_an_svg_logo_refuses_with_a_specific_actionable_message(self) -> None:
        service, repo, blobs = _service()
        asset = await service.upload(administration_id=ADMIN, actor_user_id=ACTOR, data=CLEAN_SVG)

        with pytest.raises(LogoNotRenderable, match="SVG"):
            await resolve_logo_image(
                repository=repo, blobs=blobs, administration_id=ADMIN, asset_id=asset.id
            )

    async def test_a_vanished_asset_refuses_rather_than_crashing(self) -> None:
        _, repo, blobs = _service()
        with pytest.raises(LogoNotRenderable):
            await resolve_logo_image(
                repository=repo, blobs=blobs, administration_id=ADMIN, asset_id=uuid.uuid4()
            )
