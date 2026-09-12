"""FR-TPL-008's central proof: issue-time rendering and preview rendering
produce BYTE-IDENTICAL output, given the same `InvoiceView` + `InvoiceTemplate`
+ `SupplierDetails`.

This is the single most important test in the invoice-PDF-rendering change.
Before it, `api.templates.rendering.MinimalTemplatePreviewRenderer` and
`api.invoicing.rendering.MinimalPdfRenderer` were two separate classes with no
relationship to each other - the exact violation FR-TPL-008 forbids. After it,
both call sites resolve to the SAME `api.invoicing.rendering.
TemplatedPdfRenderer.render(...)` coroutine, and this test proves that by
calling BOTH real call paths (`api.invoicing.posting.SalesPostingService.
_store_rendering`, the issue-time path, and `api.templates.service.
InvoiceTemplateService.preview`, the preview path) with equivalent inputs and
asserting the bytes each one produces are identical.

--- Why the inputs are "equivalent" rather than "the exact same request" ---

Preview has no real invoice behind it (FR-TPL-008's own wording: "what is
previewed is what is sent" is about the ENGINE, not about there happening to
be a real invoice on screen already). `api.templates.service._sample_view`
is the ONE place a fabricated, deterministic `InvoiceView` is built for
exactly this reason - imported directly here so the "issue" side of this
comparison renders that SAME fabricated view too. That is not cheating the
test: it is what makes the comparison isolate the renderer's behaviour from
the fact that preview's data happens to be fictional. The template, the
supplier, the formatting locale and the document type are IDENTICAL on both
sides - the properties an author actually controls when they preview
"what I am about to save."
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import replace

import pytest

from api.authz.model import AuthorizationDecision
from api.i18n.language import Language
from api.invoicing.posting import SalesPostingService
from api.invoicing.rendering import TemplatedPdfRenderer, default_template
from api.invoicing.statutory import SupplierDetails
from api.templates.model import (
    BlockText,
    ColorScheme,
    ColumnLayout,
    ColumnSetting,
    ContentBlock,
    DocumentType,
    InvoiceTemplate,
    LineColumn,
    Logo,
    LogoPosition,
    LogoSize,
    Typography,
)
from api.templates.service import (
    InvoiceTemplateService,
    TemplateFields,
    _sample_view,
    encode_preview,
)

pytestmark = pytest.mark.anyio

ADMIN = uuid.uuid4()
ORG = uuid.uuid4()
ACTOR = uuid.uuid4()

SUPPLIER = SupplierDetails(
    legal_name="Bakker Consultancy B.V.",
    address_line1="Damrak 70",
    address_line2=None,
    postal_code="1012 LM",
    city="Amsterdam",
    country="NL",
    vat_number="NL123456789B01",
    kvk_number="12345678",
)
LOCALE = "nl-NL"


def _custom_template() -> InvoiceTemplate:
    """A REAL configured template, not the trivial built-in default - custom
    colours, a reordered/partially-hidden column set (still statutorily
    compliant: quantity, unit price and VAT rate stay visible) and content
    blocks that actually use merge tags. Byte-identity has to hold for THIS,
    not only for a template nobody has touched.
    """
    base = default_template(organization_id=ORG, administration_id=ADMIN)
    return replace(
        base,
        name="Huisstijl 2026",
        colors=ColorScheme(accent="#1d4ed8", text="#0f172a", background="#ffffff"),
        typography=Typography(
            heading_font="ibm_plex_sans", body_font="ibm_plex_sans", figures_font="ibm_plex_mono"
        ),
        columns=ColumnLayout(
            (
                ColumnSetting(column=LineColumn.QUANTITY, visible=True, position=1),
                ColumnSetting(column=LineColumn.UNIT, visible=False, position=2),
                ColumnSetting(column=LineColumn.UNIT_PRICE, visible=True, position=3),
                ColumnSetting(column=LineColumn.DISCOUNT, visible=False, position=4),
                ColumnSetting(column=LineColumn.VAT_RATE, visible=True, position=5),
                ColumnSetting(column=LineColumn.LINE_TOTAL, visible=True, position=6),
            )
        ),
        blocks=(
            BlockText(
                block=ContentBlock.HEADER,
                text_nl="Bedankt voor uw vertrouwen, {{customer_name}}.",
                text_en="Thank you for your trust, {{customer_name}}.",
            ),
            BlockText(block=ContentBlock.INTRO, text_nl="", text_en=""),
            BlockText(
                block=ContentBlock.PAYMENT_TERMS,
                text_nl="Gelieve te betalen voor {{due_date}}.",
                text_en="Please pay before {{due_date}}.",
            ),
            BlockText(block=ContentBlock.FOOTER, text_nl="", text_en=""),
            BlockText(
                block=ContentBlock.LEGAL_IDENTITY,
                text_nl="KvK {{supplier_kvk_number}} — btw-nr. {{supplier_vat_number}}",
                text_en="KvK {{supplier_kvk_number}} — VAT no. {{supplier_vat_number}}",
            ),
        ),
    )


class _FakeTemplateRepository:
    """Only the three methods `InvoiceTemplateService.preview` actually
    calls on its repository (`_require`'s authorization check is faked
    separately) - `get`, `supplier`, `formatting_locale`.
    """

    def __init__(self, template: object, supplier: SupplierDetails, locale: str) -> None:
        self._template = template
        self._supplier = supplier
        self._locale = locale

    async def get(self, *, administration_id: uuid.UUID, template_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self._template

    async def supplier(self, *, administration_id: uuid.UUID) -> SupplierDetails | None:
        return self._supplier

    async def formatting_locale(self, *, administration_id: uuid.UUID) -> str:
        return self._locale


class _AllowAllAuthorization:
    async def authorize(self, request: object) -> AuthorizationDecision:
        return AuthorizationDecision(allowed=True, reason="allowed")


class _FakePostingRepository:
    """Only what `SalesPostingService._store_rendering` calls."""

    def __init__(self, template: object, supplier: SupplierDetails, locale: str) -> None:
        self._template = template
        self._supplier = supplier
        self._locale = locale

    async def supplier(self, *, administration_id: uuid.UUID) -> SupplierDetails | None:
        return self._supplier

    async def formatting_locale(self, *, administration_id: uuid.UUID) -> str:
        return self._locale

    async def default_invoice_template(self, *, administration_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self._template

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return ORG


class _CapturingDocuments:
    """Captures the bytes `_store_rendering` uploads - the real issue-time
    path's rendered output, with nothing about storage under test here.
    """

    def __init__(self) -> None:
        self.data: bytes | None = None

    async def upload(self, *, data: bytes, **_: object) -> None:
        # `_store_rendering`'s return value (a `Document`) is not read by
        # this test - only the BYTES it renders and hands to `upload` are
        # under test here, so a real `Document` is not worth constructing.
        self.data = data
        return None


def _fields_from(template: object) -> TemplateFields:
    return TemplateFields(
        name=template.name,
        is_default=template.is_default,
        layout=template.layout,
        logo=template.logo,
        typography=template.typography,
        colors=template.colors,
        columns=template.columns,
        blocks=template.blocks,
    )


async def test_preview_and_issue_render_byte_identical_pdfs() -> None:
    """The proof. Same template, same supplier, same locale, same document
    type, same (fabricated-for-preview, reused-for-issue) invoice data -
    through the TWO REAL call paths - produces the TWO IDENTICAL results.
    """
    template = _custom_template()

    # -- the preview path: api.templates.service.InvoiceTemplateService --
    preview_service = InvoiceTemplateService(
        repository=_FakeTemplateRepository(template, SUPPLIER, LOCALE),  # type: ignore[arg-type]
        authorization=_AllowAllAuthorization(),  # type: ignore[arg-type]
        audit_log=None,  # type: ignore[arg-type]  # never reached: authorization always allows
        renderer=TemplatedPdfRenderer(),
    )
    preview_result = await preview_service.preview(
        administration_id=ADMIN,
        template_id=template.id,
        actor_user_id=ACTOR,
        fields=_fields_from(template),
        document_type=DocumentType.INVOICE,
        language=Language.NL,
    )
    preview_bytes = base64.b64decode(encode_preview(preview_result))

    # -- the issue path: api.invoicing.posting.SalesPostingService --------
    documents = _CapturingDocuments()
    posting_service = SalesPostingService(
        repository=_FakePostingRepository(template, SUPPLIER, LOCALE),  # type: ignore[arg-type]
        ledger=None,  # type: ignore[arg-type]  # _store_rendering never touches the ledger
        documents=documents,  # type: ignore[arg-type]
        renderer=TemplatedPdfRenderer(),
    )
    issue_view = _sample_view(administration_id=ADMIN, organization_id=ORG, language=Language.NL)
    await posting_service._store_rendering(issue_view, actor_user_id=ACTOR, correlation_id=None)
    issue_bytes = documents.data

    assert issue_bytes is not None
    assert issue_bytes == preview_bytes


async def test_the_same_renderer_class_is_reachable_from_both_call_sites() -> None:
    """Not "two renderers that happen to agree today" - literally one class.
    `api.templates.rendering` (the module the old, violating
    `MinimalTemplatePreviewRenderer` lived in) no longer exists.
    """
    import importlib.util

    assert importlib.util.find_spec("api.templates.rendering") is None
    # The module the OLD, FR-TPL-008-violating `MinimalTemplatePreviewRenderer`
    # lived in is gone entirely - not merely unused. The real structural proof
    # is `test_preview_and_issue_render_byte_identical_pdfs` above, where both
    # services are constructed with the SAME `TemplatedPdfRenderer` class.


async def test_a_changed_setting_changes_the_preview_bytes() -> None:
    """FR-TPL-008's OTHER half: "live preview updates as settings change."
    A renderer that produced identical bytes regardless of the template would
    trivially pass the byte-identity test above for the wrong reason.
    """
    template = _custom_template()
    changed = replace(
        template, colors=ColorScheme(accent="#dc2626", text="#0f172a", background="#ffffff")
    )

    async def render_with(candidate: object) -> bytes:
        service = InvoiceTemplateService(
            repository=_FakeTemplateRepository(candidate, SUPPLIER, LOCALE),  # type: ignore[arg-type]
            authorization=_AllowAllAuthorization(),  # type: ignore[arg-type]
            audit_log=None,  # type: ignore[arg-type]
            renderer=TemplatedPdfRenderer(),
        )
        result = await service.preview(
            administration_id=ADMIN,
            template_id=candidate.id,
            actor_user_id=ACTOR,
            fields=_fields_from(candidate),
            document_type=DocumentType.INVOICE,
            language=Language.NL,
        )
        return result.content

    original_bytes = await render_with(template)
    changed_bytes = await render_with(changed)
    assert original_bytes != changed_bytes


async def test_a_logo_free_template_omits_the_gap_the_old_preview_could_not_close() -> None:
    """`Logo` position/size are honoured when a `resolve_logo` hook is wired
    (see `api.invoicing.rendering.TemplatedPdfRenderer`'s docstring on what
    is and is not end-to-end today) - here, with none wired (the honest
    default), a logo-configured template still renders without one, rather
    than failing or fabricating a placeholder image.
    """
    template = replace(
        _custom_template(),
        logo=Logo(asset_id=uuid.uuid4(), position=LogoPosition.RIGHT, size=LogoSize.LARGE),
    )
    service = InvoiceTemplateService(
        repository=_FakeTemplateRepository(template, SUPPLIER, LOCALE),  # type: ignore[arg-type]
        authorization=_AllowAllAuthorization(),  # type: ignore[arg-type]
        audit_log=None,  # type: ignore[arg-type]
        renderer=TemplatedPdfRenderer(),
    )
    result = await service.preview(
        administration_id=ADMIN,
        template_id=template.id,
        actor_user_id=ACTOR,
        fields=_fields_from(template),
        document_type=DocumentType.INVOICE,
        language=Language.NL,
    )
    assert result.content.startswith(b"%PDF-")
    assert b"/Figure" not in result.content
