"""Invoice template designer HTTP surface - FR-TPL-001..007, FR-TPL-008,
FR-TPL-009, FR-TPL-012, FR-TPL-013.

Registered via `register(app)` rather than `include_router`, for the reason
`api.invoicing.routes.register` gives at length: this FastAPI version hides an
included router's routes behind a wrapper the authorization middleware and
every coverage check walk straight past.

--- Same permission as sales invoice drafting, reused verbatim ---

Every mutating and reading route here declares the IDENTICAL
`require_permission("create", "sales_invoice", ...)` dependency
`api.invoicing.routes.create_invoice` / `get_invoice` declare - not a
template-shaped permission of its own. See `api.templates.service`'s
docstring and `docs/decisions/ADR-041-invoice-template-model.md` for why:
Appendix A has no row for templates and ADR-012 forbids inventing one, so
this reuses the row that already covers "shaping what a sales invoice looks
like" - `create sales_invoice`.

--- Amounts do not cross this wire; colours and merge tags do, as plain strings ---

Unlike `api.invoicing.routes`, nothing here is a Decimal - a template has no
monetary figures of its own. What DOES need the same "never let a machine
value hide inside a translated sentence" discipline is FR-TPL-009's
violations: each one travels as `{field, language, message}`, the identical
shape `api.invoicing.routes._view_json` already uses for
`statutory_failures`.
"""

from __future__ import annotations

import re
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
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
from api.db import get_db_session
from api.documents.routes import verify_separate_origin_configured
from api.documents.scanning import build_scanner
from api.i18n.http import problem, request_language
from api.i18n.language import Language, parse_language
from api.invoicing.rendering import build_invoice_renderer
from api.templates.asset_content_type import TemplateAssetContentTypeError
from api.templates.assets import (
    MAX_UPLOAD_BYTES,
    LogoNotRenderable,
    TemplateAssetInfected,
    TemplateAssetInvalidSvg,
    TemplateAssetNotFound,
    TemplateAssetScanUnavailable,
    TemplateAssetService,
    TemplateAssetTooLarge,
    build_administration_blob_store,
    build_resolve_logo,
)
from api.templates.compliance import NotCompliant, TemplateViolation, describe
from api.templates.model import (
    FONT_CODES,
    BlockText,
    ColorScheme,
    ColumnLayout,
    ColumnSetting,
    ContentBlock,
    DocumentType,
    FontWeight,
    HeaderArrangement,
    InvoiceTemplate,
    Layout,
    LetterSpacing,
    LineColumn,
    LineHeight,
    Logo,
    LogoPosition,
    LogoSize,
    Margins,
    PageSize,
    StaleTemplateVersion,
    TemplateConflict,
    TemplateNameConflict,
    TemplateNotFound,
    TotalsPosition,
    TypeScale,
    Typography,
)
from api.templates.repository import SqlInvoiceTemplateRepository
from api.templates.service import (
    InvoiceTemplateService,
    PreviewResult,
    TemplateFields,
    encode_preview,
)
from api.tenancy import TenantContext, get_tenant_context

_HEX_COLOR = re.compile(r"^#[0-9a-f]{6}$")


def register(app: FastAPI) -> None:
    app.add_api_route(
        "/v1/administrations/{administration_id}/invoice-templates",
        list_templates,
        methods=["GET"],
        name="list_invoice_templates",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/invoice-templates",
        create_template,
        methods=["POST"],
        name="create_invoice_template",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/invoice-templates/{template_id}",
        get_template,
        methods=["GET"],
        name="get_invoice_template",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/invoice-templates/{template_id}",
        update_template,
        methods=["PUT"],
        name="update_invoice_template",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/invoice-templates/{template_id}/preview",
        preview_template,
        methods=["POST"],
        name="preview_invoice_template",
    )
    # FR-TPL-019: one-click duplicate and reset.
    app.add_api_route(
        "/v1/administrations/{administration_id}/invoice-templates/{template_id}/duplicate",
        duplicate_template,
        methods=["POST"],
        name="duplicate_invoice_template",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/invoice-templates/{template_id}/reset",
        reset_template,
        methods=["POST"],
        name="reset_invoice_template",
    )
    # FR-TPL-001, FR-TPL-018. A logo is uploaded once and referenced by id from
    # a template's `logo.asset_id` (LogoBody below) - it is not part of the
    # TemplateBody payload itself, the same "upload first, reference by id"
    # shape api.documents.routes takes for a source document.
    app.add_api_route(
        "/v1/administrations/{administration_id}/template-assets",
        upload_template_asset,
        methods=["POST"],
        name="upload_template_asset",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/template-assets/{asset_id}",
        download_template_asset,
        methods=["GET"],
        name="download_template_asset",
    )


async def get_template_service(
    administration_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> InvoiceTemplateService:
    audit_log = AuditLog(SqlAuditRepository(session))
    return InvoiceTemplateService(
        repository=SqlInvoiceTemplateRepository(session),
        authorization=authorization,
        audit_log=audit_log,
        # FR-TPL-008: the SAME provider selection, AND the same `resolve_logo`
        # construction `api.invoicing.routes.get_invoicing_service` uses at
        # issue - see `api.templates.assets.build_resolve_logo`'s docstring.
        # Not literally the same Python object (a fresh instance per request,
        # like every other dependency built by this function), but the same
        # class constructed the same way, which is what "one engine" means
        # for a stateless renderer with no per-instance state that could
        # differ between the two.
        renderer=build_invoice_renderer(
            settings.invoice_renderer_provider,
            resolve_logo=build_resolve_logo(administration_id=administration_id, session=session),
        ),
    )


async def get_template_asset_service(
    administration_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> TemplateAssetService:
    """Built per request and already scoped to one administration - the same
    shape `api.documents.routes.get_document_service` takes, for the same
    reason: the blob store inside it is constructed around THIS
    administration's key.
    """
    from api.templates.asset_repository import SqlTemplateAssetRepository

    return TemplateAssetService(
        repository=SqlTemplateAssetRepository(session),
        blobs=build_administration_blob_store(administration_id=administration_id, session=session),
        scanner=build_scanner(settings.malware_scanner_provider),
        authorization=authorization,
        audit_log=AuditLog(SqlAuditRepository(session)),
    )


# --- request bodies -----------------------------------------------------------


class LogoBody(BaseModel):
    asset_id: uuid.UUID | None = None
    position: str = "left"
    size: str = "medium"


class TypographyBody(BaseModel):
    #: FR-TPL-002. Free strings, validated against `FONT_CODES` below rather
    #: than by pydantic, so an unknown code gets FR-TPL-009's-sibling D5
    #: treatment (a field name and a list of what IS accepted) instead of
    #: pydantic's generic validation error shape.
    heading_font: str = "ibm_plex_sans"
    body_font: str = "ibm_plex_sans"
    figures_font: str = "ibm_plex_mono"
    type_scale: str = "medium"
    font_weight: str = "regular"
    line_height: str = "normal"
    letter_spacing: str = "normal"


class ColorsBody(BaseModel):
    accent: str = "#0f172a"
    text: str = "#111827"
    background: str = "#ffffff"


class ColumnBody(BaseModel):
    column: str
    visible: bool = True
    position: int


class BlockBody(BaseModel):
    block: str
    text_nl: str = ""
    text_en: str = ""


class TemplateBody(BaseModel):
    """FR-TPL-001..007's designed object, on the wire.

    `columns` and `blocks` default to a compliant scaffold when omitted (see
    `_default_columns` / `_default_blocks`), so `{"name": "My template"}`
    alone is enough to create a usable, already-compliant template - FR-TPL-
    019's "reset to default" is the same scaffold, reachable today by
    creating a new template rather than through a dedicated action.
    """

    name: str
    is_default: bool = False
    layout: str = "classic"
    logo: LogoBody = Field(default_factory=LogoBody)
    typography: TypographyBody = Field(default_factory=TypographyBody)
    colors: ColorsBody = Field(default_factory=ColorsBody)
    columns: list[ColumnBody] | None = None
    blocks: list[BlockBody] | None = None
    #: FR-TPL-005. Independent of `layout` - see `api.templates.model.
    #: HeaderArrangement`'s docstring.
    header_arrangement: str = "split"
    #: FR-TPL-005.
    totals_position: str = "right"
    #: FR-TPL-010.
    page_size: str = "a4"
    #: FR-TPL-010.
    margins: str = "normal"


class TemplateUpdateBody(TemplateBody):
    #: 0042's optimistic-concurrency column, read back from a prior GET and
    #: sent back unchanged unless the caller means to overwrite somebody
    #: else's more recent save.
    version: int


class ResetTemplateBody(BaseModel):
    """FR-TPL-019's reset: the only thing a caller supplies is the version it
    last read, exactly like `TemplateUpdateBody`'s own `version` - every other
    field is reset to the built-in default, not sent by the caller.
    """

    version: int


class PreviewBody(TemplateBody):
    document_type: str = "invoice"
    #: The READER's choice of which of the template's two languages to
    #: preview - not negotiated from Accept-Language, because FR-TPL-013
    #: makes this a property of the DOCUMENT being previewed, and a Dutch
    #: bookkeeper previewing what an English customer will receive needs to
    #: see the English text regardless of their own UI language.
    language: str = "nl"


# --- body -> domain -------------------------------------------------------------


def _default_columns() -> ColumnLayout:
    return ColumnLayout(
        tuple(
            ColumnSetting(column=column, visible=True, position=position)
            for position, column in enumerate(LineColumn, start=1)
        )
    )


def _default_blocks() -> tuple[BlockText, ...]:
    # The legal_identity default already carries both locked merge tags, in
    # both languages - a brand-new template is compliant from the moment it
    # is created, not merely capable of becoming so.
    return (
        BlockText(block=ContentBlock.HEADER, text_nl="", text_en=""),
        BlockText(block=ContentBlock.INTRO, text_nl="", text_en=""),
        BlockText(block=ContentBlock.PAYMENT_TERMS, text_nl="", text_en=""),
        BlockText(block=ContentBlock.FOOTER, text_nl="", text_en=""),
        BlockText(
            block=ContentBlock.LEGAL_IDENTITY,
            text_nl="KvK {{supplier_kvk_number}} — btw-nr. {{supplier_vat_number}}",
            text_en="KvK {{supplier_kvk_number}} — VAT no. {{supplier_vat_number}}",
        ),
    )


def _invalid(request: Request, field: str, accepted: list[str] | None = None) -> Exception:
    extra: dict[str, object] = {"field": field}
    if accepted is not None:
        extra["accepted"] = accepted
    return problem(
        request, 422, "errors.customer_field_invalid", reason="customer_field_invalid", **extra
    )


def _logo(request: Request, body: LogoBody) -> Logo:
    try:
        position = LogoPosition(body.position)
    except ValueError as exc:
        raise _invalid(request, "logo.position", [m.value for m in LogoPosition]) from exc
    try:
        size = LogoSize(body.size)
    except ValueError as exc:
        raise _invalid(request, "logo.size", [m.value for m in LogoSize]) from exc
    return Logo(asset_id=body.asset_id, position=position, size=size)


def _typography(request: Request, body: TypographyBody) -> Typography:
    for field_name in ("heading_font", "body_font", "figures_font"):
        if getattr(body, field_name) not in FONT_CODES:
            raise _invalid(request, f"typography.{field_name}", sorted(FONT_CODES))
    try:
        type_scale = TypeScale(body.type_scale)
    except ValueError as exc:
        raise _invalid(request, "typography.type_scale", [m.value for m in TypeScale]) from exc
    try:
        font_weight = FontWeight(body.font_weight)
    except ValueError as exc:
        raise _invalid(request, "typography.font_weight", [m.value for m in FontWeight]) from exc
    try:
        line_height = LineHeight(body.line_height)
    except ValueError as exc:
        raise _invalid(request, "typography.line_height", [m.value for m in LineHeight]) from exc
    try:
        letter_spacing = LetterSpacing(body.letter_spacing)
    except ValueError as exc:
        raise _invalid(
            request, "typography.letter_spacing", [m.value for m in LetterSpacing]
        ) from exc
    return Typography(
        heading_font=body.heading_font,
        body_font=body.body_font,
        figures_font=body.figures_font,
        type_scale=type_scale,
        font_weight=font_weight,
        line_height=line_height,
        letter_spacing=letter_spacing,
    )


def _colors(request: Request, body: ColorsBody) -> ColorScheme:
    for field_name, value in (
        ("accent", body.accent),
        ("text", body.text),
        ("background", body.background),
    ):
        if not _HEX_COLOR.match(value):
            raise _invalid(request, f"colors.{field_name}")
    return ColorScheme(accent=body.accent, text=body.text, background=body.background)


def _columns(request: Request, bodies: list[ColumnBody] | None) -> ColumnLayout:
    if bodies is None:
        return _default_columns()
    settings: list[ColumnSetting] = []
    for body in bodies:
        try:
            column = LineColumn(body.column)
        except ValueError as exc:
            raise _invalid(request, "columns.column", [m.value for m in LineColumn]) from exc
        settings.append(ColumnSetting(column=column, visible=body.visible, position=body.position))
    try:
        return ColumnLayout(tuple(settings))
    except ValueError as exc:
        raise _invalid(request, "columns") from exc


def _blocks(request: Request, bodies: list[BlockBody] | None) -> tuple[BlockText, ...]:
    if bodies is None:
        return _default_blocks()
    result: list[BlockText] = []
    for body in bodies:
        try:
            block = ContentBlock(body.block)
        except ValueError as exc:
            raise _invalid(request, "blocks.block", [m.value for m in ContentBlock]) from exc
        result.append(BlockText(block=block, text_nl=body.text_nl, text_en=body.text_en))
    present = sorted(b.block.value for b in result)
    expected = sorted(m.value for m in ContentBlock)
    if present != expected:
        raise _invalid(request, "blocks", expected)
    return tuple(result)


def _fields(request: Request, body: TemplateBody) -> TemplateFields:
    try:
        layout = Layout(body.layout)
    except ValueError as exc:
        raise _invalid(request, "layout", [m.value for m in Layout]) from exc
    try:
        header_arrangement = HeaderArrangement(body.header_arrangement)
    except ValueError as exc:
        raise _invalid(request, "header_arrangement", [m.value for m in HeaderArrangement]) from exc
    try:
        totals_position = TotalsPosition(body.totals_position)
    except ValueError as exc:
        raise _invalid(request, "totals_position", [m.value for m in TotalsPosition]) from exc
    try:
        page_size = PageSize(body.page_size)
    except ValueError as exc:
        raise _invalid(request, "page_size", [m.value for m in PageSize]) from exc
    try:
        margins = Margins(body.margins)
    except ValueError as exc:
        raise _invalid(request, "margins", [m.value for m in Margins]) from exc
    return TemplateFields(
        name=body.name,
        is_default=body.is_default,
        layout=layout,
        logo=_logo(request, body.logo),
        typography=_typography(request, body.typography),
        colors=_colors(request, body.colors),
        columns=_columns(request, body.columns),
        blocks=_blocks(request, body.blocks),
        header_arrangement=header_arrangement,
        totals_position=totals_position,
        page_size=page_size,
        margins=margins,
    )


# --- domain -> wire -------------------------------------------------------------


def _template_json(template: InvoiceTemplate) -> dict[str, object]:
    return {
        "id": str(template.id),
        "administration_id": str(template.administration_id),
        "name": template.name,
        "is_default": template.is_default,
        "version": template.version,
        "layout": template.layout.value,
        "header_arrangement": template.header_arrangement.value,
        "totals_position": template.totals_position.value,
        "page_size": template.page_size.value,
        "margins": template.margins.value,
        "logo": {
            "asset_id": str(template.logo.asset_id) if template.logo.asset_id else None,
            "position": template.logo.position.value,
            "size": template.logo.size.value,
        },
        "typography": {
            "heading_font": template.typography.heading_font,
            "body_font": template.typography.body_font,
            "figures_font": template.typography.figures_font,
            "type_scale": template.typography.type_scale.value,
            "font_weight": template.typography.font_weight.value,
            "line_height": template.typography.line_height.value,
            "letter_spacing": template.typography.letter_spacing.value,
        },
        "colors": {
            "accent": template.colors.accent,
            "text": template.colors.text,
            "background": template.colors.background,
            # FR-TPL-004: advisory only, never blocks a save - always
            # returned so a client can show it inline beside the pickers.
            "contrast_warnings": sorted(w.value for w in template.colors.contrast_warnings),
        },
        "columns": [
            {
                "column": setting.column.value,
                "visible": setting.visible,
                "position": setting.position,
            }
            for setting in sorted(template.columns.settings, key=lambda setting: setting.position)
        ],
        "blocks": [
            {"block": block.block.value, "text_nl": block.text_nl, "text_en": block.text_en}
            for block in template.blocks
        ],
        "updated_by_user_id": (
            str(template.updated_by_user_id) if template.updated_by_user_id else None
        ),
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }


def _violations_json(
    violations: tuple[TemplateViolation, ...], language: Language
) -> list[dict[str, object]]:
    return [
        {
            "field": violation.field.value,
            "language": violation.language.value if violation.language is not None else None,
            "message": message,
        }
        for violation, message in describe(violations, language)
    ]


def _not_compliant(request: Request, exc: NotCompliant) -> Exception:
    return problem(
        request,
        422,
        "errors.invoice_template_incomplete",
        reason="invoice_template_incomplete",
        count=len(exc.violations),
        violations=_violations_json(exc.violations, request_language(request)),
    )


# --- routes ----------------------------------------------------------------


def _require_authenticated(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


async def list_templates(
    administration_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoiceTemplateService = Depends(get_template_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create", "sales_invoice", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    """FR-TPL-011's list - mostly one row in P0, shaped for more later."""
    user_id = _require_authenticated(request, tenant)
    templates = await service.list_templates(
        administration_id=administration_id, actor_user_id=user_id
    )
    return {"templates": [_template_json(template) for template in templates]}


async def get_template(
    administration_id: uuid.UUID,
    template_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoiceTemplateService = Depends(get_template_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create", "sales_invoice", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    user_id = _require_authenticated(request, tenant)
    try:
        template = await service.get_template(
            administration_id=administration_id, template_id=template_id, actor_user_id=user_id
        )
    except TemplateNotFound as exc:
        raise problem(
            request, 404, "errors.invoice_template_not_found", reason="invoice_template_not_found"
        ) from exc
    return _template_json(template)


async def create_template(
    administration_id: uuid.UUID,
    body: TemplateBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoiceTemplateService = Depends(get_template_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-TPL-001..007: a new template. Refuses (422) rather than saves a
    non-compliant one - FR-TPL-009.
    """
    user_id = _require_authenticated(request, tenant)
    fields = _fields(request, body)
    try:
        template = await service.create_template(
            administration_id=administration_id, actor_user_id=user_id, fields=fields
        )
    except NotCompliant as exc:
        raise _not_compliant(request, exc) from exc
    except TemplateConflict as exc:
        raise problem(
            request, 409, "errors.invoice_template_conflict", reason="invoice_template_conflict"
        ) from exc
    except TemplateNameConflict as exc:
        raise problem(
            request,
            409,
            "errors.invoice_template_name_conflict",
            reason="invoice_template_name_conflict",
        ) from exc
    except TemplateNotFound as exc:
        raise problem(
            request, 404, "errors.administration_not_found", reason="administration_not_found"
        ) from exc
    return _template_json(template)


async def update_template(
    administration_id: uuid.UUID,
    template_id: uuid.UUID,
    body: TemplateUpdateBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoiceTemplateService = Depends(get_template_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-TPL-001..007's edit. Must run FR-TPL-009's compliance gate before
    persisting - non-compliant is a 422 with a `violations` array carrying
    each failure's own `{field, language, message}`, the shape `sales-
    invoices`' `issue` route uses for `missing_fields`.
    """
    user_id = _require_authenticated(request, tenant)
    fields = _fields(request, body)
    try:
        template = await service.update_template(
            administration_id=administration_id,
            template_id=template_id,
            actor_user_id=user_id,
            expected_version=body.version,
            fields=fields,
        )
    except NotCompliant as exc:
        raise _not_compliant(request, exc) from exc
    except TemplateConflict as exc:
        raise problem(
            request, 409, "errors.invoice_template_conflict", reason="invoice_template_conflict"
        ) from exc
    except TemplateNameConflict as exc:
        raise problem(
            request,
            409,
            "errors.invoice_template_name_conflict",
            reason="invoice_template_name_conflict",
        ) from exc
    except StaleTemplateVersion as exc:
        raise problem(
            request,
            409,
            "errors.invoice_template_stale_version",
            reason="invoice_template_stale_version",
        ) from exc
    except TemplateNotFound as exc:
        raise problem(
            request, 404, "errors.invoice_template_not_found", reason="invoice_template_not_found"
        ) from exc
    return _template_json(template)


async def duplicate_template(
    administration_id: uuid.UUID,
    template_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoiceTemplateService = Depends(get_template_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-TPL-019: one click, one atomic server-side copy - see
    `api.templates.service.InvoiceTemplateService.duplicate_template`. No
    request body: everything the new row needs comes from the source
    template.
    """
    user_id = _require_authenticated(request, tenant)
    try:
        template = await service.duplicate_template(
            administration_id=administration_id, template_id=template_id, actor_user_id=user_id
        )
    except TemplateNameConflict as exc:
        raise problem(
            request,
            409,
            "errors.invoice_template_name_conflict",
            reason="invoice_template_name_conflict",
        ) from exc
    except TemplateNotFound as exc:
        raise problem(
            request, 404, "errors.invoice_template_not_found", reason="invoice_template_not_found"
        ) from exc
    return _template_json(template)


async def reset_template(
    administration_id: uuid.UUID,
    template_id: uuid.UUID,
    body: ResetTemplateBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoiceTemplateService = Depends(get_template_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-TPL-019: one click, back to the built-in default design - see
    `api.templates.service.InvoiceTemplateService.reset_template`. `id`,
    `name` and `is_default` are untouched; `version` is bumped exactly like an
    ordinary update, so a stale `version` is refused the same way.
    """
    user_id = _require_authenticated(request, tenant)
    try:
        template = await service.reset_template(
            administration_id=administration_id,
            template_id=template_id,
            actor_user_id=user_id,
            expected_version=body.version,
        )
    except NotCompliant as exc:  # pragma: no cover - defaults are compliant by construction
        raise _not_compliant(request, exc) from exc
    except StaleTemplateVersion as exc:
        raise problem(
            request,
            409,
            "errors.invoice_template_stale_version",
            reason="invoice_template_stale_version",
        ) from exc
    except TemplateNotFound as exc:
        raise problem(
            request, 404, "errors.invoice_template_not_found", reason="invoice_template_not_found"
        ) from exc
    return _template_json(template)


async def preview_template(
    administration_id: uuid.UUID,
    template_id: uuid.UUID,
    body: PreviewBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoiceTemplateService = Depends(get_template_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            # IAM-090: a POST that reveals the administration's real VAT and
            # KvK numbers (merged into the legal_identity preview - see
            # api.templates.service.InvoiceTemplateService.preview) even
            # though it persists nothing. tests/test_audit_coverage.py
            # requires every POST/PUT/PATCH/DELETE to declare a category or
            # be explicitly exempt, and "reveals statutory identity data" is
            # not "changes nothing an auditor would ask about" - the bar
            # that file's AUDIT_EXEMPT_PATHS sets.
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-TPL-008: renders the payload in the request body - a possibly not-
    yet-saved draft - rather than the stored row, through the SAME
    `api.invoicing.rendering.TemplatedPdfRenderer` invoice issue uses. See
    `api.templates.service.InvoiceTemplateService.preview` and
    `api.invoicing.rendering`'s module docstring for the byte-identity proof.

    Not gated by FR-TPL-009: a draft mid-edit may be momentarily non-
    compliant, and `violations` in the response says so without refusing to
    render - refusing belongs to `create`/`update`, which persist.
    """
    user_id = _require_authenticated(request, tenant)
    fields = _fields(request, body)

    try:
        document_type = DocumentType(body.document_type)
    except ValueError as exc:
        raise _invalid(request, "document_type", [m.value for m in DocumentType]) from exc

    language = parse_language(body.language)
    if language is None:
        raise _invalid(request, "language", [m.value for m in Language])

    try:
        result: PreviewResult = await service.preview(
            administration_id=administration_id,
            template_id=template_id,
            actor_user_id=user_id,
            fields=fields,
            document_type=document_type,
            language=language,
        )
    except TemplateNotFound as exc:
        raise problem(
            request, 404, "errors.invoice_template_not_found", reason="invoice_template_not_found"
        ) from exc
    except LogoNotRenderable as exc:
        # 409, the identical posture and identical reason
        # `api.invoicing.routes.issue_invoice` uses for the same exception:
        # nothing about this REQUEST is wrong, the template's logo is not
        # renderable today (an SVG - see LogoNotRenderable's docstring), and
        # the fix is to change the template, not to retry the preview.
        raise problem(
            request,
            409,
            "errors.invoice_logo_not_renderable",
            reason="invoice_logo_not_renderable",
        ) from exc

    return {
        "document_type": document_type.value,
        "language": language.value,
        "content_type": result.content_type,
        "filename": result.filename,
        "content_base64": encode_preview(result),
        "compliant": not result.violations,
        "violations": _violations_json(result.violations, request_language(request)),
    }


# --- template assets: FR-TPL-001, FR-TPL-018 --------------------------------


def _asset_json(asset: object) -> dict[str, object]:
    """One shape for the upload response. `storage_key` is deliberately
    absent - the same reason `api.documents.routes._document_json` omits it:
    an internal blob reference is not something a client should hold, since
    holding it would invite fetching the blob directly, bypassing the
    download endpoint's own SEC-005 headers.
    """
    return {
        "id": str(asset.id),  # type: ignore[attr-defined]
        "content_type": asset.content_type.value,  # type: ignore[attr-defined]
        "sanitized": asset.sanitized,  # type: ignore[attr-defined]
    }


async def upload_template_asset(
    administration_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: TemplateAssetService = Depends(get_template_asset_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            # IAM-090: uploading a logo changes what every future invoice
            # from this administration looks like - a configuration change,
            # the same category `create_invoice_template` itself carries.
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-TPL-001's logo upload, FR-TPL-018's sanitisation gate, SEC-005.

    The body is the raw file, with `Content-Type` describing it - the same
    shape `api.documents.routes.upload_document` takes and for the identical
    reason (see that function's docstring): multipart needs a parser running
    over attacker-controlled bytes before anything has decided to accept
    them, and a raw PUT-style body costs a client nothing (`fetch(url,
    {method: 'POST', body: file, headers: {'Content-Type': file.type}})`).

    Idempotency is enforced globally by `IdempotencyMiddleware` for every
    mutating method (NFR-032) - this route needs no opt-in of its own.
    """
    user_id = _require_authenticated(request, tenant)

    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > MAX_UPLOAD_BYTES:
        raise problem(
            request,
            413,
            "errors.template_asset_too_large",
            reason="template_asset_too_large",
            limit_bytes=MAX_UPLOAD_BYTES,
        )

    data = await request.body()
    if len(data) > MAX_UPLOAD_BYTES:
        raise problem(
            request,
            413,
            "errors.template_asset_too_large",
            reason="template_asset_too_large",
            limit_bytes=MAX_UPLOAD_BYTES,
        )

    try:
        asset = await service.upload(
            administration_id=administration_id,
            actor_user_id=user_id,
            data=data,
            declared_content_type=request.headers.get("content-type"),
        )
    except TemplateAssetTooLarge as exc:
        raise problem(
            request,
            413,
            "errors.template_asset_too_large",
            reason="template_asset_too_large",
            limit_bytes=exc.limit,
        ) from exc
    except TemplateAssetContentTypeError as exc:
        raise problem(
            request,
            415,
            "errors.template_asset_type_rejected",
            reason="template_asset_type_rejected",
        ) from exc
    except TemplateAssetInvalidSvg as exc:
        # 422: the upload is a well-formed request naming an SVG file, and
        # the FILE is what is refused - FR-TPL-018's own sanitiser reason,
        # already D5-shaped, travels through rather than being replaced with
        # a generic sentence.
        raise problem(
            request,
            422,
            "errors.template_asset_svg_rejected",
            reason="template_asset_svg_rejected",
            svg_reason=exc.reason,
        ) from exc
    except TemplateAssetInfected as exc:
        raise problem(
            request,
            422,
            "errors.template_asset_infected",
            reason="template_asset_infected",
        ) from exc
    except TemplateAssetScanUnavailable as exc:
        raise problem(
            request,
            503,
            "errors.template_asset_scan_unavailable",
            reason="template_asset_scan_unavailable",
        ) from exc

    return _asset_json(asset)


async def download_template_asset(
    administration_id: uuid.UUID,
    asset_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: TemplateAssetService = Depends(get_template_asset_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> Response:
    """The stored logo bytes - SEC-005's last two clauses, mirroring
    `api.documents.routes.download_document` exactly: `Content-Disposition:
    attachment` so a browser saves rather than renders, and the same
    configured-separate-origin enforcement (see
    `api.documents.routes.verify_separate_origin_configured`) rather than a
    second, independently-drifting copy of that check.

    `TemplateDesigner.tsx` never points an `<img src>` at this URL directly -
    it calls this via `fetch()` and builds an object URL from the Blob, which
    is what makes the attachment disposition irrelevant to the browser's own
    rendering of the logo in the designer (see that component's docstring).
    """
    user_id = _require_authenticated(request, tenant)
    verify_separate_origin_configured()

    try:
        asset, data = await service.download(
            administration_id=administration_id, asset_id=asset_id, actor_user_id=user_id
        )
    except TemplateAssetNotFound as exc:
        raise problem(
            request,
            404,
            "errors.template_asset_not_found",
            reason="template_asset_not_found",
        ) from exc

    return Response(
        content=data,
        media_type=asset.content_type.value,
        headers={
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, no-store",
        },
    )
