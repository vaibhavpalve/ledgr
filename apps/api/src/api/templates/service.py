"""Invoice template designer service - FR-TPL-001..007, FR-TPL-009,
FR-TPL-012, FR-TPL-013.

--- Same permission as sales invoice drafting, deliberately (ADR-041, ADR-012) ---

There is no "manage invoice_template" row in Appendix A, and this module does
not invent one. Saving a template is authorised by the SAME grant that lets
somebody draft a sales invoice - `create sales_invoice` - reusing
`api.invoicing.service.CREATE_INVOICE` verbatim (the same tuple object, not a
copy of its two strings) so the two checks cannot silently drift into two
different permissions that happen to read the same today. ADR-012 forbids
inventing a permission outside Appendix A; ADR-041 argues that branding a
sales invoice is part of what "create sales_invoice" already means, not a
capability of its own.

--- Compliance is checked here, not only at the route ---

`create_template` and `update_template` both run `api.templates.compliance.
check()` before persisting, for the same reason `api.invoicing.service.issue`
runs `api.invoicing.statutory.check` before allocating a number: FR-TPL-009
says a template CANNOT BE SAVED in a non-compliant state, and the route's job
is translating the resulting `NotCompliant` into a 422 (with each violation's
field and message, the same `missing_fields`-shaped array FR-AR-003 already
returns), not deciding whether to call this method with a bad payload.
`preview` runs the same check but does NOT raise on it - FR-TPL-008 wants a
live preview of whatever is currently on screen, including a draft that is
mid-edit and momentarily non-compliant, so the violations travel back as data
alongside the rendered bytes instead of blocking the render.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.i18n.language import Language
from api.invoicing.model import (
    InvoiceLine,
    InvoiceStatus,
    InvoiceView,
    NotAuthorizedToInvoice,
    SalesInvoice,
)
from api.invoicing.rendering import InvoiceRenderer
from api.invoicing.service import CREATE_INVOICE
from api.invoicing.statutory import SupplierDetails
from api.invoicing.vat import VatGroup
from api.templates.compliance import NotCompliant, TemplateViolation, check
from api.templates.model import (
    BlockText,
    ColorScheme,
    ColumnLayout,
    ColumnSetting,
    ContentBlock,
    DocumentType,
    HeaderArrangement,
    InvoiceTemplate,
    Layout,
    LineColumn,
    Logo,
    Margins,
    PageSize,
    StaleTemplateVersion,
    TemplateNameConflict,
    TemplateNotFound,
    TotalsPosition,
    Typography,
)
from api.vat.rules import TreatmentRole

#: FR-TPL-008's preview has no real invoice behind it - fixed, clearly
#: fictitious sample data, deterministic across calls (never `date.today()` or
#: `uuid.uuid4()`) so that previewing the SAME template twice in a row
#: produces the SAME bytes, the same determinism guarantee
#: `api.invoicing.pdf`'s module docstring holds the real rendering path to.
_SAMPLE_INVOICE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_SAMPLE_FISCAL_YEAR_ID = uuid.UUID("00000000-0000-0000-0000-0000000000fa")
_SAMPLE_INVOICE_DATE = date(2026, 1, 1)
_SAMPLE_DUE_DATE = date(2026, 1, 31)
_SAMPLE_CUSTOMER_NAME = "Voorbeeld Klant B.V."
_SAMPLE_CUSTOMER_ADDRESS = "Voorbeeldstraat 1\n1234 AB  Amsterdam"
_SAMPLE_NET = Decimal("1000.00")
_SAMPLE_VAT_RATE = Decimal("21.00")
_SAMPLE_VAT = Decimal("210.00")

#: FR-TPL-019's duplicate: how many disambiguated names ("Copy of X (2)",
#: "(3)", ...) `duplicate_template` tries before giving up. Colliding this
#: many times in practice would mean this exact copy already exists several
#: times over, which is exceedingly rare.
_MAX_DUPLICATE_NAME_ATTEMPTS = 20


def _default_columns() -> ColumnLayout:
    """The same compliant scaffold `api.templates.routes._default_columns`
    and `api.invoicing.rendering._default_columns` give - duplicated rather
    than imported (importing from `api.templates.routes` here would be
    circular: that module already imports from this one) for the identical
    reason `api.invoicing.rendering`'s own copy gives.
    """
    return ColumnLayout(
        tuple(
            ColumnSetting(column=column, visible=True, position=position)
            for position, column in enumerate(LineColumn, start=1)
        )
    )


def _default_blocks() -> tuple[BlockText, ...]:
    """`api.templates.routes._default_blocks`'s mirror - see `_default_columns` above."""
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


def _sample_view(
    *, administration_id: uuid.UUID, organization_id: uuid.UUID, language: Language
) -> InvoiceView:
    """A fabricated, deterministic `InvoiceView` for FR-TPL-008's preview -
    the ONLY place preview's sample data lives. `TemplatedPdfRenderer` itself
    has no idea this view is not real; that is the whole point of the fix -
    see `api.invoicing.rendering`'s module docstring.

    The description is a plain, untranslated placeholder rather than a
    catalogue key: it is fabricated preview filler, not customer-facing
    statutory content, so FR-LOC-001's "every user-facing string comes from
    the catalogue" does not reach it - the same posture the OLD (deleted)
    `api.templates.rendering`'s sample merge-tag values already took.
    """
    line = InvoiceLine(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000ab"),
        position=1,
        description="Voorbeeldregel" if language.value == "nl" else "Sample line",
        quantity=Decimal("1.00"),
        unit_price=_SAMPLE_NET,
        discount_percent=Decimal("0.00"),
        vat_treatment="btw_21",
        role=TreatmentRole.STANDARD,
        line_net=_SAMPLE_NET,
    )
    group = VatGroup("btw_21", TreatmentRole.STANDARD, _SAMPLE_VAT_RATE, _SAMPLE_NET, _SAMPLE_VAT)
    invoice = SalesInvoice(
        id=_SAMPLE_INVOICE_ID,
        organization_id=organization_id,
        administration_id=administration_id,
        fiscal_year_id=_SAMPLE_FISCAL_YEAR_ID,
        status=InvoiceStatus.ISSUED,
        invoice_number=1,
        number_prefix="PREVIEW-",
        invoice_reference="PREVIEW-0001",
        invoice_date=_SAMPLE_INVOICE_DATE,
        supply_date=None,
        due_date=_SAMPLE_DUE_DATE,
        customer_name=_SAMPLE_CUSTOMER_NAME,
        customer_address=_SAMPLE_CUSTOMER_ADDRESS,
        customer_country="NL",
        customer_vat_number=None,
        customer_id=None,
        customer_language=language,
        # Never a real "credits" relationship - which document heading a
        # preview shows is driven by the `document_type` PARAMETER
        # `TemplatedPdfRenderer` receives, not by this property. See
        # `api.invoicing.rendering._title`.
        credits_invoice_id=None,
        notes=None,
        issued_at=None,
        lines=(line,),
    )
    return InvoiceView(
        invoice=invoice,
        groups=(group,),
        net=_SAMPLE_NET,
        vat=_SAMPLE_VAT,
        gross=_SAMPLE_NET + _SAMPLE_VAT,
    )


@dataclass(frozen=True, slots=True)
class TemplateFields:
    """Everything a create or update supplies about a template's content -
    `api.invoicing.service.NewLine`'s counterpart, bundled because create and
    update both take the identical shape and a route builds one from its
    request body either way.
    """

    name: str
    is_default: bool
    layout: Layout
    logo: Logo
    typography: Typography
    colors: ColorScheme
    columns: ColumnLayout
    blocks: tuple[BlockText, ...]
    header_arrangement: HeaderArrangement = HeaderArrangement.SPLIT
    totals_position: TotalsPosition = TotalsPosition.RIGHT
    page_size: PageSize = PageSize.A4
    margins: Margins = Margins.NORMAL


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """FR-TPL-008's rendered bytes, plus FR-TPL-009's advisory-not-blocking
    read of the same draft. `violations` is empty exactly when the draft
    could ALSO be saved as-is.
    """

    content: bytes
    content_type: str
    filename: str
    violations: tuple[TemplateViolation, ...]


class InvoiceTemplateRepository(Protocol):
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        name: str,
        is_default: bool,
        layout: Layout,
        logo: Logo,
        typography: Typography,
        colors: ColorScheme,
        columns: ColumnLayout,
        blocks: Sequence[BlockText],
        header_arrangement: HeaderArrangement,
        totals_position: TotalsPosition,
        page_size: PageSize,
        margins: Margins,
        user_id: uuid.UUID,
    ) -> InvoiceTemplate: ...

    async def get(
        self, *, administration_id: uuid.UUID, template_id: uuid.UUID
    ) -> InvoiceTemplate | None: ...

    async def list_for_administration(
        self, *, administration_id: uuid.UUID
    ) -> tuple[InvoiceTemplate, ...]: ...

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        template_id: uuid.UUID,
        expected_version: int,
        name: str,
        is_default: bool,
        layout: Layout,
        logo: Logo,
        typography: Typography,
        colors: ColorScheme,
        columns: ColumnLayout,
        blocks: Sequence[BlockText],
        header_arrangement: HeaderArrangement,
        totals_position: TotalsPosition,
        page_size: PageSize,
        margins: Margins,
        user_id: uuid.UUID,
    ) -> InvoiceTemplate | None: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def supplier(self, *, administration_id: uuid.UUID) -> SupplierDetails | None: ...

    async def formatting_locale(self, *, administration_id: uuid.UUID) -> str:
        """FR-LOC-002, resolved for `preview` the same way
        `api.invoicing.posting_repository.SqlSalesPostingRepository.
        formatting_locale` resolves it for issue - duplicated rather than
        shared, the same posture this Protocol's `supplier` already takes and
        for the same reason (`api.templates.repository`'s docstring): a
        template preview reading through the invoicing package would be the
        cross-package coupling CLAUDE.md's fourth non-negotiable warns
        against. Without this, a preview could format figures differently
        from the invoice it is a preview OF - a second, quieter way FR-TPL-008
        could be broken even with one render function.
        """
        ...


class InvoiceTemplateService:
    def __init__(
        self,
        repository: InvoiceTemplateRepository,
        authorization: AuthorizationService,
        audit_log: AuditLog,
        renderer: InvoiceRenderer,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit = audit_log
        self._renderer = renderer

    # -- reading --------------------------------------------------------------

    async def list_templates(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> tuple[InvoiceTemplate, ...]:
        await self._require(actor_user_id, administration_id)
        return await self._repository.list_for_administration(administration_id=administration_id)

    async def get_template(
        self, *, administration_id: uuid.UUID, template_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> InvoiceTemplate:
        await self._require(actor_user_id, administration_id)
        return await self._get_or_refuse(administration_id, template_id)

    # -- writing ----------------------------------------------------------------

    async def create_template(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        fields: TemplateFields,
    ) -> InvoiceTemplate:
        """FR-TPL-001..007. Refuses (`NotCompliant`) rather than saves a
        template that hides or omits FR-TPL-009's locked content - checked
        against a throwaway candidate BEFORE the insert, the same
        gate-before-write order `api.invoicing.service.issue` uses for
        FR-AR-003.
        """
        await self._require(actor_user_id, administration_id)
        organization_id = await self._organization_of(administration_id)

        candidate = InvoiceTemplate(
            id=uuid.uuid4(),
            organization_id=organization_id,
            administration_id=administration_id,
            name=fields.name,
            is_default=fields.is_default,
            version=1,
            layout=fields.layout,
            logo=fields.logo,
            typography=fields.typography,
            colors=fields.colors,
            columns=fields.columns,
            blocks=fields.blocks,
            header_arrangement=fields.header_arrangement,
            totals_position=fields.totals_position,
            page_size=fields.page_size,
            margins=fields.margins,
        )
        violations = check(candidate)
        if violations:
            raise NotCompliant(violations)

        template = await self._repository.create(
            organization_id=organization_id,
            administration_id=administration_id,
            name=fields.name,
            is_default=fields.is_default,
            layout=fields.layout,
            logo=fields.logo,
            typography=fields.typography,
            colors=fields.colors,
            columns=fields.columns,
            blocks=fields.blocks,
            header_arrangement=fields.header_arrangement,
            totals_position=fields.totals_position,
            page_size=fields.page_size,
            margins=fields.margins,
            user_id=actor_user_id,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="create_invoice_template",
            resource_id=template.id,
            detail={"name": template.name, "is_default": template.is_default},
        )
        return template

    async def update_template(
        self,
        *,
        administration_id: uuid.UUID,
        template_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        expected_version: int,
        fields: TemplateFields,
    ) -> InvoiceTemplate:
        """FR-TPL-001..007's edit. Same compliance gate as `create_template`,
        and the same optimistic-concurrency check the route's `version` field
        exists for (0042) - two edits started from the same version cannot
        both win silently.
        """
        await self._require(actor_user_id, administration_id)
        current = await self._get_or_refuse(administration_id, template_id)

        if current.version != expected_version:
            raise StaleTemplateVersion(template_id, expected_version)

        candidate = InvoiceTemplate(
            id=template_id,
            organization_id=current.organization_id,
            administration_id=administration_id,
            name=fields.name,
            is_default=fields.is_default,
            version=current.version,
            layout=fields.layout,
            logo=fields.logo,
            typography=fields.typography,
            colors=fields.colors,
            columns=fields.columns,
            blocks=fields.blocks,
            header_arrangement=fields.header_arrangement,
            totals_position=fields.totals_position,
            page_size=fields.page_size,
            margins=fields.margins,
        )
        violations = check(candidate)
        if violations:
            raise NotCompliant(violations)

        updated = await self._repository.update(
            administration_id=administration_id,
            template_id=template_id,
            expected_version=expected_version,
            name=fields.name,
            is_default=fields.is_default,
            layout=fields.layout,
            logo=fields.logo,
            typography=fields.typography,
            colors=fields.colors,
            columns=fields.columns,
            blocks=fields.blocks,
            header_arrangement=fields.header_arrangement,
            totals_position=fields.totals_position,
            page_size=fields.page_size,
            margins=fields.margins,
            user_id=actor_user_id,
        )
        if updated is None:
            # Lost a race between the read above and this write - somebody
            # else's save landed in between. Same refusal as the version
            # mismatch caught earlier, reported honestly rather than retried
            # silently: retrying would overwrite whatever they just saved.
            raise StaleTemplateVersion(template_id, expected_version)

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="update_invoice_template",
            resource_id=template_id,
            detail={"name": updated.name, "version": updated.version},
        )
        return updated

    # -- FR-TPL-019: duplicate and reset --------------------------------------

    async def duplicate_template(
        self, *, administration_id: uuid.UUID, template_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> InvoiceTemplate:
        """FR-TPL-019's "duplicate an existing template" as one atomic
        server-side action: every field of `template_id` (layout, logo -
        including `asset_id`, so the copy keeps its source's logo - typography,
        colours, columns, blocks and the four new layout/page-setup axes)
        lands in a brand-new row, `is_default=False` (a duplicate never steals
        FR-TPL-011's one-default-per-administration slot from the original)
        and `version=1`.

        The name is `"Copy of {name}"`, then `"Copy of {name} (2)"`, `"(3)"`,
        ... up to `_MAX_DUPLICATE_NAME_ATTEMPTS` tries - `0043`'s
        `invoice_template_name_unique` is what makes a collision possible at
        all, and colliding more than a handful of times in practice would mean
        somebody has already made this exact copy several times over, which is
        exceedingly rare.
        """
        await self._require(actor_user_id, administration_id)
        source = await self._get_or_refuse(administration_id, template_id)

        last_conflict: TemplateNameConflict | None = None
        for attempt in range(1, _MAX_DUPLICATE_NAME_ATTEMPTS + 1):
            name = (
                f"Copy of {source.name}" if attempt == 1 else f"Copy of {source.name} ({attempt})"
            )
            try:
                duplicate = await self._repository.create(
                    organization_id=source.organization_id,
                    administration_id=administration_id,
                    name=name,
                    is_default=False,
                    layout=source.layout,
                    logo=source.logo,
                    typography=source.typography,
                    colors=source.colors,
                    columns=source.columns,
                    blocks=source.blocks,
                    header_arrangement=source.header_arrangement,
                    totals_position=source.totals_position,
                    page_size=source.page_size,
                    margins=source.margins,
                    user_id=actor_user_id,
                )
            except TemplateNameConflict as exc:
                last_conflict = exc
                continue

            await self._record(
                administration_id=administration_id,
                user_id=actor_user_id,
                action="duplicate_invoice_template",
                resource_id=duplicate.id,
                detail={"source_template_id": str(template_id), "name": duplicate.name},
            )
            return duplicate

        assert last_conflict is not None  # the loop always assigns it before exhausting attempts
        raise last_conflict

    async def reset_template(
        self,
        *,
        administration_id: uuid.UUID,
        template_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        expected_version: int,
    ) -> InvoiceTemplate:
        """FR-TPL-019's "reset to default", in place: `layout`, `logo`,
        `typography`, `colors`, `columns`, `blocks` and the four new axes go
        back to the built-in defaults - `id`, `name` and `is_default` are
        untouched, because a reset is "put this template's DESIGN back to
        square one," not "rename it or un-default it."

        Goes through the exact same `update_template` save path (compliance
        gate, optimistic concurrency) rather than writing the row directly, so
        a future change to what "default" means cannot silently produce a
        non-compliant reset target without the existing gate catching it - the
        defaults are compliant by construction today, and this is what keeps
        that true automatically rather than by two places agreeing to agree.
        """
        current = await self.get_template(
            administration_id=administration_id,
            template_id=template_id,
            actor_user_id=actor_user_id,
        )
        return await self.update_template(
            administration_id=administration_id,
            template_id=template_id,
            actor_user_id=actor_user_id,
            expected_version=expected_version,
            fields=TemplateFields(
                name=current.name,
                is_default=current.is_default,
                layout=Layout.CLASSIC,
                logo=Logo(asset_id=None),
                typography=Typography(
                    heading_font="ibm_plex_sans",
                    body_font="ibm_plex_sans",
                    figures_font="ibm_plex_mono",
                ),
                colors=ColorScheme(accent="#0f172a", text="#111827", background="#ffffff"),
                columns=_default_columns(),
                blocks=_default_blocks(),
                header_arrangement=HeaderArrangement.SPLIT,
                totals_position=TotalsPosition.RIGHT,
                page_size=PageSize.A4,
                margins=Margins.NORMAL,
            ),
        )

    # -- FR-TPL-008: preview --------------------------------------------------

    async def preview(
        self,
        *,
        administration_id: uuid.UUID,
        template_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        fields: TemplateFields,
        document_type: DocumentType,
        language: Language,
    ) -> PreviewResult:
        """Renders a NOT-YET-SAVED draft, from the request body rather than
        from the stored row - `template_id` only anchors the request to a
        real template in this administration (so the same authorization and
        tenancy checks apply as every other route here), and is not read for
        its content.

        FR-TPL-008: this calls `self._renderer.render(...)` - the exact same
        coroutine `api.invoicing.posting.SalesPostingService._store_rendering`
        calls at issue - with a FABRICATED `InvoiceView` (`_sample_view`,
        above) standing in for a real invoice. The renderer itself cannot tell
        the difference, which is the point: "the same engine" is true of the
        CODE, not merely similar in appearance.
        """
        await self._require(actor_user_id, administration_id)
        current = await self._get_or_refuse(administration_id, template_id)

        candidate = InvoiceTemplate(
            id=template_id,
            organization_id=current.organization_id,
            administration_id=administration_id,
            name=fields.name,
            is_default=fields.is_default,
            version=current.version,
            layout=fields.layout,
            logo=fields.logo,
            typography=fields.typography,
            colors=fields.colors,
            columns=fields.columns,
            blocks=fields.blocks,
            header_arrangement=fields.header_arrangement,
            totals_position=fields.totals_position,
            page_size=fields.page_size,
            margins=fields.margins,
        )
        # Advisory here, unlike create/update: FR-TPL-008 wants a live preview
        # of whatever is on screen right now, including a draft that is
        # mid-edit and momentarily non-compliant. The violations travel back
        # as data so a client can show them inline WITHOUT the render itself
        # refusing - the render is what proves to the author what "hiding the
        # VAT rate column" actually looks like.
        violations = check(candidate)

        supplier = await self._repository.supplier(
            administration_id=administration_id
        ) or SupplierDetails(
            legal_name=None,
            address_line1=None,
            postal_code=None,
            city=None,
            vat_number=None,
            kvk_number=None,
        )
        locale = await self._repository.formatting_locale(administration_id=administration_id)
        view = _sample_view(
            administration_id=administration_id,
            organization_id=current.organization_id,
            language=language,
        )
        rendered = await self._renderer.render(
            view,
            supplier=supplier,
            formatting_locale=locale,
            template=candidate,
            document_type=document_type,
        )
        return PreviewResult(
            content=rendered.content,
            content_type=rendered.content_type,
            filename=rendered.filename,
            violations=violations,
        )

    # -- internals -------------------------------------------------------------

    async def _get_or_refuse(
        self, administration_id: uuid.UUID, template_id: uuid.UUID
    ) -> InvoiceTemplate:
        template = await self._repository.get(
            administration_id=administration_id, template_id=template_id
        )
        if template is None:
            raise TemplateNotFound(f"invoice template {template_id} not found")
        return template

    async def _organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise TemplateNotFound(f"administration {administration_id} does not exist")
        return organization_id

    async def _require(self, user_id: uuid.UUID, administration_id: uuid.UUID) -> None:
        """Authorises against `CREATE_INVOICE` - `("create", "sales_invoice")`
        - the IDENTICAL Appendix A permission `api.invoicing.service` checks
        before drafting an invoice. See this module's docstring.
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
            await self._record(
                administration_id=administration_id,
                user_id=user_id,
                action=f"{action}_{resource_type}",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise NotAuthorizedToInvoice(action, resource_type, decision.detail or decision.reason)

    async def _record(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        resource_id: uuid.UUID,
        detail: dict[str, object],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
    ) -> None:
        organization_id = await self._organization_of(administration_id)
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="invoice_template",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                detail=detail,
            )
        )


def encode_preview(result: PreviewResult) -> str:
    """Base64, for the JSON envelope `api.templates.routes` returns.

    A raw binary response is the more usual shape for a PDF and is what
    FR-TPL-008's eventual client almost certainly wants for an inline
    preview - but SEC-005's separate-origin, attachment-disposition control
    belongs to a dedicated download endpoint the way `api.documents.routes`
    already builds one, and building a second one for an EPHEMERAL preview
    render is out of scope for this pass. A JSON envelope keeps this route
    consistent with every other one in this package (a dict body) and keeps
    the violations travelling beside the bytes in one response rather than a
    header a client would have to know to look for.
    """
    return base64.b64encode(result.content).decode("ascii")
