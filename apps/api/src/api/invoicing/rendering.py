"""Rendering an invoice to the bytes a customer receives - FR-TPL-017, and
FR-TPL-008's "no separate preview renderer."

    FR-TPL-008  Live preview updates as settings change, rendered from the
                SAME ENGINE that produces the final PDF - what is previewed
                is what is sent. NO SEPARATE PREVIEW RENDERER.

--- FR-TPL-008 was violated here, and this module is the fix ---

A prior pass built `api.templates.rendering.MinimalTemplatePreviewRenderer`,
reachable only from `POST .../invoice-templates/{id}/preview`, alongside this
module's own `MinimalPdfRenderer`, reachable only from invoice issue. Two
classes, two code paths, one drawing an invoice and the other drawing a
template preview - which is exactly the "separate preview renderer" FR-TPL-008
forbids, however real and well-intentioned each one was on its own.

This module now holds the ONE renderer both call sites use:
`TemplatedPdfRenderer`. `api.templates.routes.preview_template` and
`api.invoicing.posting.SalesPostingService._store_rendering` both end up
calling the same `render()` on the same class, with the same
`InvoiceTemplate` shape either way - a real, saved template at issue time, an
in-memory, possibly-unsaved candidate at preview time (`api.templates.
model.InvoiceTemplate` does not care which). `api.templates.rendering` is
deleted; see `docs/decisions/ADR-042-invoice-pdf-rendering.md` for the design
and `tests/templates/test_render_parity.py` for the byte-identity proof.

--- The requirement is ALSO about storage, independently of the above ---

Most of FR-TPL-017's own content is a claim about WHEN rendering happens and
what becomes of the result. An invoice is rendered exactly once, at issue,
and those bytes are what every later read returns. There is no re-render
path - not a lazy one, not a cache, not an admin tool - because a re-render is
the only way a template edit could reach a document already sent, and the
absence of the path is what makes the requirement true rather than merely
intended.

Migration 0040 holds the other half: `sales_invoice.document_id` is set once and
a trigger refuses to move it. The bytes live in the document archive (0031),
where they are encrypted under the administration's own key, hash-verified on
every read (FR-DOC-001), and retained for CMP-001's seven years.

--- What `TemplatedPdfRenderer` draws, and what it honestly does not ---

  * **Columns** (FR-TPL-006): the six `api.templates.model.LineColumn`
    members, shown/hidden/ordered exactly as `template.columns` says.
  * **Content blocks** (FR-TPL-007): all five, with merge tags substituted
    from the SAME `InvoiceView`/`SupplierDetails` every other figure on the
    page comes from - never a hardcoded sample. A preview's "sample data" is
    entirely `api.templates.service.InvoiceTemplateService.preview`'s
    business (a synthetic `InvoiceView`), never this renderer's.
  * **Logo** (FR-TPL-001): drawn when a caller supplies `resolve_logo` (see
    below) and it returns bytes; `LogoPosition`/`LogoSize` are honoured.
  * **Typography** (FR-TPL-002/003): applied ONLY to the extent real font
    embedding is configured (`embedded_fonts`, built from
    `api.invoicing.truetype`) - with no embedded font for a chosen
    `template_font` code, this renderer falls back to a base-14 face mapped
    by `FontWeight`, and says so via `_font_for` rather than silently
    pretending the chosen typeface was used. Type scale, line height and
    letter spacing are NOT yet applied - see Known gaps in ADR-042.
  * **Colour** (FR-TPL-004): `colors.text` is the body/heading fill colour,
    `colors.accent` colours the rules and the totals row, and
    `colors.background` becomes a filled band behind the page header WHEN it
    is not white - `api.invoicing.pdf` gained `Color`/`Rect` for exactly this
    (it had no colour operator at all before this change).
  * **FR-TPL-016 holds for every new mark**: every block/column/logo goes
    through `Page.at`/`Page.place_image`, never a raw dict - see
    `tests/templates/test_render_parity.py` and `tests/invoicing/
    test_rendering.py`'s structure assertions.
  * **Layout, header arrangement, totals position, page size and margins**
    (FR-TPL-005/FR-TPL-010, ADR-045): `template.layout` picks a starting
    point with its own visual character (`_LAYOUT_CHARACTER`);
    `header_arrangement`/`totals_position` are independently adjustable from
    it; `page_size`/`margins` resolve to this template's own page dimensions
    and margin, shadowing this module's `_MARGIN`/`_RIGHT`/`_PAGE_BOTTOM`
    constants for the rest of `_lay_out` - see that function's own comment at
    the shadowing assignment. Multi-page invoices print a carried-forward /
    brought-forward running subtotal across every page break.

--- Two languages, two locales, and they are not the same choice ---

    WORDS      the recipient's language (FR-TPL-013), snapshotted onto the
               invoice as `customer_language` by migration 0039.
    FIGURES    the ADMINISTRATION's formatting locale (FR-LOC-002) - how
               figures in these books are written, for every reader.

So a Dutch business invoicing a German customer in English gets English labels
and Dutch number formatting: `Total  € 1.234,56`. That looks odd written down
and is what both requirements ask for - the words are for the reader, and the
figures are the books' own.

--- The legal wording is content, not decoration ---

Art. 226(11) requires an invoice to state WHY no VAT was charged. `InvoiceView`
carries that per treatment, already resolved into the recipient's language, and
this renderer prints it. An invoice showing 0,00 with no explanation is not
compliant, and the customer cannot act on it: a reverse-charged supply obliges
them to account for the VAT and they can only know because the document says so.

`wording_is_provisional` is deliberately NOT printed. It is an internal warning
that no Dutch tax adviser has signed the sentence off (see
`api.invoicing.wording`), and a note to that effect on a customer's invoice
would be worse than the risk it describes.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from api.i18n.catalogue import translate
from api.i18n.formatting import format_date, format_money, format_number, locale_spec
from api.i18n.language import Language
from api.invoicing.epc_qr import EpcQrRequest, render_epc_qr
from api.invoicing.model import InvoiceLine, InvoiceView
from api.invoicing.pdf import (
    A4_HEIGHT,
    A4_WIDTH,
    LETTER_HEIGHT,
    LETTER_WIDTH,
    Color,
    EmbeddedFont,
    Font,
    Image,
    Page,
    StructureTag,
    load_image,
    render_pdf,
    text_width,
    winansi_codepoints,
    wrap,
)
from api.invoicing.statutory import SupplierDetails
from api.invoicing.truetype import TrueTypeError, build_embedded_font, parse_font
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
    TotalsPosition,
    TypeScale,
    Typography,
)

__all__ = [
    "InvoiceRenderer",
    "RenderedInvoice",
    "TemplatedPdfRenderer",
    "build_invoice_renderer",
    "default_template",
]

# --- page geometry, in points ------------------------------------------------
_MARGIN = 56.0
_RIGHT = 595.0 - _MARGIN
_BODY = 9.5
_SMALL = 9.0
_HEADING = 18.0
_LINE_HEIGHT = 13.0
#: Where the line table stops and the footer begins. A page break happens
#: before this rather than after, so nothing is ever drawn under the footer.
_PAGE_BOTTOM = 760.0
#: Every toggleable column shares one slot width, measured leftward from
#: `_RIGHT` - see `_column_edges`.
_COLUMN_SLOT = 82.0
#: The nil id a synthetic `default_template()` carries - never a real primary
#: key (0042 generates those with `gen_random_uuid()`), so it can never collide
#: with a saved row.
_DEFAULT_TEMPLATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

_MERGE_TAG = re.compile(r"\{\{(\w+)\}\}")

#: Column header catalogue keys - FR-TPL-006's six, in `LineColumn` order.
_COLUMN_LABEL_KEYS: dict[LineColumn, str] = {
    LineColumn.QUANTITY: "invoice.pdf.column_quantity",
    LineColumn.UNIT: "invoice.pdf.column_unit",
    LineColumn.UNIT_PRICE: "invoice.pdf.column_unit_price",
    LineColumn.DISCOUNT: "invoice.pdf.column_discount",
    LineColumn.VAT_RATE: "invoice.pdf.column_vat_rate",
    LineColumn.LINE_TOTAL: "invoice.pdf.column_amount",
}

#: Heading catalogue keys - only the two document kinds actually built
#: (FR-AR-001) have one; the rest of FR-TPL-012's family (quote, order
#: confirmation, reminder, statement) are later PRD sections with no rendered
#: form yet, so `_title` falls back to a plain label for them rather than
#: raising - a template preview should show SOMETHING for every member of the
#: family it claims to apply to.
_DOCUMENT_TITLE_KEYS: dict[DocumentType, str] = {
    DocumentType.INVOICE: "invoice.pdf.invoice_title",
    DocumentType.CREDIT_NOTE: "invoice.pdf.credit_note_title",
}

#: `LogoSize` (FR-TPL-001) as a display height in points; width follows from
#: `Image.fit`'s aspect-ratio preservation.
_LOGO_HEIGHT: dict[LogoSize, float] = {
    LogoSize.SMALL: 28.0,
    LogoSize.MEDIUM: 42.0,
    LogoSize.LARGE: 64.0,
}

_WHITE = "#ffffff"

# --- FR-TPL-003: type scale, line height and letter spacing, as explicit,
# curated tables - never a formula, so "what does `large` actually draw at"
# is answered by reading this file rather than by computing it. Each table is
# keyed by the enum `api.templates.model.Typography` already restricts a
# template to, so there is nothing here for a caller to get wrong beyond
# picking one of the enum's own members.
#
# `medium`/`normal` (every field's own default, and what `default_template`
# and every template created before this change already carries) reproduces
# today's pre-existing `_BODY`/`_SMALL`/`_HEADING`/`_LINE_HEIGHT` constants
# EXACTLY - see `tests/invoicing/test_rendering.py` for the byte-identity
# proof this is what keeps every existing document's appearance unchanged.
# -----------------------------------------------------------------------------

#: The size role most of an invoice's body text already draws at
#: (`_SMALL`/`small_size` below). It is ALREADY at FR-TPL-016's 9pt floor at
#: `medium`, so `small` cannot go any smaller than `medium` here - there is no
#: headroom below the floor to offer as a step, and `Text.__post_init__`
#: would refuse it outright if this table tried.
_TYPE_SCALE_SMALL: dict[TypeScale, float] = {
    TypeScale.SMALL: _SMALL,
    TypeScale.MEDIUM: _SMALL,
    TypeScale.LARGE: 10.0,
    TypeScale.EXTRA_LARGE: 11.0,
}

#: The size role used for the supplier name, the customer name, the invoice
#: reference and the totals row (`_BODY`/`body_size` below) - a half-point
#: above `_SMALL` at every step it can be, since it is meant to read as
#: slightly more prominent than ordinary body text.
_TYPE_SCALE_BODY: dict[TypeScale, float] = {
    TypeScale.SMALL: _SMALL,  # 9.5pt has no room to shrink further while
    # staying above `_SMALL`'s own 9.0pt at this same step - see `small`'s row
    # in the table above for the actual floor.
    TypeScale.MEDIUM: _BODY,
    TypeScale.LARGE: 10.5,
    TypeScale.EXTRA_LARGE: 12.0,
}

#: The one heading on the page (the document title) - `_HEADING`/
#: `heading_size` below. The widest range of the three, since a heading has
#: the most room to grow before it risks the right margin (see `_lay_out`'s
#: own right-alignment of the title, which measures at whatever size this
#: table picks).
_TYPE_SCALE_HEADING: dict[TypeScale, float] = {
    TypeScale.SMALL: 14.0,
    TypeScale.MEDIUM: _HEADING,
    TypeScale.LARGE: 22.0,
    TypeScale.EXTRA_LARGE: 26.0,
}

#: A multiplier on `_LINE_HEIGHT`, not a fixed point value - so every row
#: advance in `_lay_out` (there are many: supplier block, customer block,
#: line items, totals, every wrapped content block) grows or shrinks together
#: from the one constant, the same "change it in one place" property
#: `_LINE_HEIGHT` itself already had.
_LINE_HEIGHT_MULTIPLIER: dict[LineHeight, float] = {
    LineHeight.TIGHT: 0.85,
    LineHeight.NORMAL: 1.0,
    LineHeight.RELAXED: 1.25,
}

#: `Tc` (PDF character spacing) values in points, per FR-TPL-003's three
#: curated steps. Deliberately small: FR-TPL-003 exists so a user cannot
#: produce an unreadable invoice, and a large negative `Tc` collides glyphs
#: into each other while a large positive one reads as widely-tracked display
#: type, neither of which belongs on a line-item table. `normal` is `0.0`,
#: which is what makes the "no letter spacing requested" case emit no `Tc`
#: operator at all - see `api.invoicing.pdf.Text.letter_spacing`.
_LETTER_SPACING_TC: dict[LetterSpacing, float] = {
    LetterSpacing.TIGHT: -0.2,
    LetterSpacing.NORMAL: 0.0,
    LetterSpacing.WIDE: 0.5,
}

# --- FR-TPL-010: page size and margins, as explicit tables - the same
# "curated steps, never a formula" posture the typography tables above take.
# `PageSize.A4`/`Margins.NORMAL` (both defaults) resolve to exactly the
# pre-existing `595`/`842`/`56.0` this renderer always used - see
# `tests/invoicing/test_rendering.py`'s byte-identity proof.
# -----------------------------------------------------------------------------

_PAGE_DIMENSIONS: dict[PageSize, tuple[int, int]] = {
    PageSize.A4: (A4_WIDTH, A4_HEIGHT),
    PageSize.LETTER: (LETTER_WIDTH, LETTER_HEIGHT),
}

#: `Margins.NORMAL` is today's pre-existing `_MARGIN` (56pt) - THE DEFAULT.
_MARGIN_BY_STEP: dict[Margins, float] = {
    Margins.NARROW: 40.0,
    Margins.NORMAL: 56.0,
    Margins.WIDE: 72.0,
}


#: FR-TPL-005's own visual differentiation for each `Layout` member - a
#: STARTING POINT's character, independent of `HeaderArrangement`/
#: `TotalsPosition` (which remain adjustable regardless of which layout was
#: picked). `CLASSIC` reproduces today's exact geometry: no background band,
#: normal spacing, and rules drawn under the header and above the totals -
#: this is the one combination that must byte-identically match pre-existing
#: output (see the byte-identity test). `MODERN` adds a tinted band behind
#: the header. `COMPACT` keeps the same structural arrangement as `CLASSIC`
#: but tightens every row-to-row gap. `MINIMAL` omits every rule and fill.
@dataclass(frozen=True, slots=True)
class _LayoutCharacter:
    uses_background_band: bool
    row_spacing_multiplier: float
    draws_rules: bool


_LAYOUT_CHARACTER: dict[Layout, _LayoutCharacter] = {
    Layout.CLASSIC: _LayoutCharacter(
        uses_background_band=False, row_spacing_multiplier=1.0, draws_rules=True
    ),
    Layout.MODERN: _LayoutCharacter(
        uses_background_band=True, row_spacing_multiplier=1.0, draws_rules=True
    ),
    Layout.COMPACT: _LayoutCharacter(
        uses_background_band=False, row_spacing_multiplier=0.8, draws_rules=True
    ),
    Layout.MINIMAL: _LayoutCharacter(
        uses_background_band=False, row_spacing_multiplier=1.0, draws_rules=False
    ),
}


def _tint(color: Color, *, toward_white: float) -> Color:
    """`color` blended `toward_white` (0.0 = unchanged, 1.0 = white) - a
    plain linear per-channel interpolation, not `ColorScheme.
    contrast_warnings`'s WCAG relative-luminance formula: that formula
    JUDGES a contrast ratio, it does not itself produce a lighter colour, and
    inventing a second colour-math path here for a decorative tint would be
    the wrong kind of thoroughness. Used at a high `toward_white` (see call
    sites) so `MODERN`'s header band and `TotalsPosition.FULL_WIDTH`'s band
    stay legible under the same dark text every other background on this page
    already uses - the same "always legible" bar this module's docstring
    holds itself to, met here by staying close to white rather than by
    reproducing the luminance maths `ColorScheme.contrast_warnings` uses to
    WARN about a bad choice elsewhere.
    """
    return Color(
        red=color.red + (1.0 - color.red) * toward_white,
        green=color.green + (1.0 - color.green) * toward_white,
        blue=color.blue + (1.0 - color.blue) * toward_white,
    )


@dataclass(frozen=True, slots=True)
class RenderedInvoice:
    """The bytes, and what they are called.

    `filename` is a suggestion for a download, built from the invoice reference
    rather than the id: a person saving three invoices wants to tell them apart
    in a folder listing.
    """

    content: bytes
    filename: str
    content_type: str = "application/pdf"


class InvoiceRenderer(Protocol):
    """CLAUDE.md non-negotiable #4's adapter seam, for FR-TPL's renderer.

    Takes an `InvoiceView` rather than an invoice id: the view already carries
    the VAT groups, the totals and the legal wording, computed once by the
    service (see `InvoiceView`'s docstring). A renderer that took an id would
    have to fetch and recompute them, and could then disagree with the invoice
    the rest of the system believes in.

    `template`/`document_type` are what closes FR-TPL-008: both the issue path
    and the preview path build one of these calls, on the one implementation
    below, and the FR-TPL-008 promise is exactly "there is only one shape of
    call that produces bytes."
    """

    async def render(
        self,
        view: InvoiceView,
        *,
        supplier: SupplierDetails,
        formatting_locale: str,
        template: InvoiceTemplate,
        document_type: DocumentType,
    ) -> RenderedInvoice: ...


class TemplatedPdfRenderer:
    """The one renderer FR-TPL-008 asks for. See the module docstring for
    exactly what it draws from `template` and what it honestly does not yet.

    `embedded_fonts` and `resolve_logo` are both optional, injected
    dependencies rather than parameters of `render()` itself - `render()`'s
    signature is fixed by `InvoiceRenderer`, which both call sites share, and
    neither dependency is a per-call fact the way `view`/`template` are:

      * `embedded_fonts` maps a `template_font.code` (FR-TPL-002) to an
        `api.invoicing.truetype.EmbeddedFont` a DEPLOYMENT has configured -
        see ADR-042 for the two data files (a font binary, an ICC profile)
        this needs and where they are read from. Empty by default, which is
        the honest state today: no licensed font ships in this repository
        (the hard constraint ADR-042 records), so every document still falls
        back to base-14 exactly as before this change.
      * `resolve_logo` is the seam FR-TPL-001's logo waits on
        `api.templates.repository`/`service` to finish wiring
        (`template_asset` fetch + blob storage, ADR-041's "Logo upload has no
        endpoint yet" gap). Given a template, it returns an already-decoded
        `api.invoicing.pdf.Image` or `None` - accepting resolved bytes rather
        than an asset id keeps this renderer free of a storage dependency,
        the same adapter posture CLAUDE.md's fourth non-negotiable asks for.
        `None` (the default) means no logo is drawn, which is the honest
        behaviour while the fetch path does not exist - never a placeholder
        image.
    """

    name = "templated-pdf"

    def __init__(
        self,
        *,
        embedded_fonts: dict[str, EmbeddedFont] | None = None,
        resolve_logo: object = None,
    ) -> None:
        self._embedded_fonts = embedded_fonts or {}
        # `object`-typed rather than `Callable[[InvoiceTemplate],
        # Awaitable[Image | None]]`: mypy would need that Protocol importable
        # from wherever a caller builds one, and `render()` below only ever
        # calls it behind an `is not None` check and an explicit
        # `type: ignore` at the one call site - a caller that passes nothing
        # (the default) pays nothing and this stays honest about being an
        # unwired seam rather than a typed contract nobody implements yet.
        self._resolve_logo = resolve_logo

    async def render(
        self,
        view: InvoiceView,
        *,
        supplier: SupplierDetails,
        formatting_locale: str,
        template: InvoiceTemplate,
        document_type: DocumentType,
    ) -> RenderedInvoice:
        invoice = view.invoice
        # FR-TPL-013: the RECIPIENT's, frozen onto the document at issue (or,
        # for a preview, whatever `InvoiceTemplateService.preview` chose).
        language = invoice.customer_language

        logo_image: Image | None = None
        if self._resolve_logo is not None and template.logo.asset_id is not None:
            logo_image = await self._resolve_logo(template)  # type: ignore[operator]

        pages = _lay_out(
            view,
            supplier,
            language,
            formatting_locale,
            template=template,
            document_type=document_type,
            logo_image=logo_image,
            embedded_fonts=self._embedded_fonts,
        )

        reference = invoice.invoice_reference or str(invoice.id)
        title = _title(document_type, language)
        embedded = _used_embedded_fonts(template, self._embedded_fonts)
        return RenderedInvoice(
            content=render_pdf(
                pages,
                # FR-TPL-016 / PDF/A 6.9: the document's OWN declared language
                # (Catalog /Lang, XMP dc:language) - the recipient's, the same
                # `language` the layout and the wording above were already
                # rendered in.
                title=f"{title} {reference}",
                language=language.value,
                embedded_fonts=tuple(embedded.values()) if embedded else (),
            ),
            filename=f"{_slug(reference)}.pdf",
        )


def build_invoice_renderer(provider: str, *, resolve_logo: object = None) -> InvoiceRenderer:
    """Selects the renderer from configuration.

    There is no "off". FR-TPL-017 requires the rendering to exist at issue, and
    an invoice issued with nothing stored is one whose appearance can never
    afterwards be established.

    `resolve_logo` is passed straight through to `TemplatedPdfRenderer` - see
    that class's docstring. Both call sites that construct a renderer for a
    real request (`api.invoicing.routes.get_invoicing_service` at issue,
    `api.templates.routes.get_template_service` at preview) build one from
    `api.templates.assets.build_resolve_logo`, so the SAME hook resolves a
    logo either way; a caller that has none (a test, or a deployment not
    wired for it yet) gets the honest `None` default - no logo drawn, never a
    placeholder image.
    """
    if provider == "minimal-pdf":
        return TemplatedPdfRenderer(
            embedded_fonts=_load_embedded_fonts(), resolve_logo=resolve_logo
        )
    raise ValueError(
        f"unknown invoice renderer provider {provider!r}. 'minimal-pdf' is the only one built."
    )


# --- FR-TPL-002's font binaries: read from disk, never fabricated -----------
#
# See ADR-042 for the full argument. In short: PDF/A and real typography both
# need a LICENSED, embeddable font binary this repository does not contain
# and will not invent one to stand in for. What follows is the loading
# mechanism, not the asset - it finds nothing today (no `.ttf` ships with
# this codebase) and `TemplatedPdfRenderer` falls back to base-14 exactly as
# it always has, honestly, via `_font_for`. A deployment closes this by
# placing licensed `<template_font.code>.ttf` files at the path
# `_font_data_directory` resolves to - no code change, the same "restart
# picks it up" contract `api.invoicing.wording`'s review file already uses.


def _font_data_directory() -> Path:
    """Most specific first - the packaged copy a deployed API reads, then the
    checkout a developer or the test suite reads. Mirrors
    `api.invoicing.wording._review_file`'s two-location search exactly.
    """
    here = Path(__file__).resolve()
    candidates = (here.parent / "data" / "fonts", here.parents[3] / "data" / "invoicing" / "fonts")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]  # need not exist - a directory with no matching files loads nothing


def _load_embedded_fonts(directory: Path | None = None) -> dict[str, EmbeddedFont]:
    """Every `template_font.code` (FR-TPL-002's curated catalogue, 0042) that
    has a `<code>.ttf` file sitting in `directory` - empty when none does,
    which is every deployment today.

    Subset to `api.invoicing.pdf.winansi_codepoints()` - this platform's own
    WinAnsi/Latin-1 text coverage - not to any one document's text: a
    renderer built once by `build_invoice_renderer` and reused for every
    invoice a process issues cannot afford to re-parse and re-subset a font
    per request for glyphs that never change, and the point of subsetting
    (excluding what a DEPLOYMENT never draws - other scripts, ligature sets,
    stylistic alternates) is preserved regardless.

    A file that exists but fails to parse (`TrueTypeError`) is skipped rather
    than crashing the process - the same "refuse the one bad row, not the
    whole batch" posture the rest of this codebase takes, and the correct one
    here: a malformed font file should fall back to base-14, not take down
    invoice issuing.
    """
    directory = directory if directory is not None else _font_data_directory()
    codepoints = winansi_codepoints()
    fonts: dict[str, EmbeddedFont] = {}
    for index, code in enumerate(sorted(FONT_CODES)):
        path = directory / f"{code}.ttf"
        if not path.is_file():
            continue
        try:
            parsed = parse_font(path.read_bytes())
            fonts[code] = build_embedded_font(
                parsed,
                # F10.. so this never collides with the base-14 F1/F2 or with
                # `render_pdf`'s own reserved-name check.
                resource_name=f"F{10 + index}".encode("ascii"),
                base_font_name=code.encode("ascii"),
                codepoints=codepoints,
            )
        except TrueTypeError:
            continue
    return fonts


# --- the built-in default, for an administration with no saved template -----


def _default_columns() -> ColumnLayout:
    """Every column visible, in `LineColumn`'s own declared order - the exact
    scaffold `api.templates.routes._default_columns` gives a brand-new
    template, so an administration that has never opened the template
    designer sees what a freshly created template would already show it,
    not a separate, narrower built-in layout.
    """
    return ColumnLayout(
        tuple(
            ColumnSetting(column=column, visible=True, position=position)
            for position, column in enumerate(LineColumn, start=1)
        )
    )


def _default_blocks() -> tuple[BlockText, ...]:
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


def default_template(
    *, organization_id: uuid.UUID, administration_id: uuid.UUID
) -> InvoiceTemplate:
    """The look an invoice gets when its administration has not saved an
    `InvoiceTemplate` of its own - `api.invoicing.posting`'s fallback so
    issuing an invoice never depends on the template designer having been
    used first.

    Not a database row: `id` is the fixed nil UUID, `version` is 0, and
    nothing here is ever persisted. Deliberately IDENTICAL in shape to what
    `POST .../invoice-templates` gives a brand-new template
    (`_default_columns`/`_default_blocks` above mirror that route's own
    scaffold) - the built-in look and "a template nobody has customised yet"
    are the same thing, not two designs to keep in step by hand.
    """
    return InvoiceTemplate(
        id=_DEFAULT_TEMPLATE_ID,
        organization_id=organization_id,
        administration_id=administration_id,
        name="Default",
        is_default=True,
        version=0,
        layout=Layout.CLASSIC,
        logo=Logo(asset_id=None),
        typography=Typography(
            heading_font="ibm_plex_sans", body_font="ibm_plex_sans", figures_font="ibm_plex_mono"
        ),
        colors=ColorScheme(accent="#0f172a", text="#111827", background=_WHITE),
        columns=_default_columns(),
        blocks=_default_blocks(),
    )


# --- layout ------------------------------------------------------------------


def _t(key: str, language: Language, **params: object) -> str:
    return translate(key, language, **params)


def _title(document_type: DocumentType, language: Language) -> str:
    key = _DOCUMENT_TITLE_KEYS.get(document_type)
    if key is not None:
        return _t(key, language)
    return document_type.value.replace("_", " ").upper()


def _slug(reference: str) -> str:
    """A filename-safe reference. Conservative on purpose - this string reaches
    a `Content-Disposition` header and a filesystem.
    """
    safe = [char if char.isalnum() or char in "-_" else "-" for char in reference]
    return "".join(safe).strip("-") or "invoice"


def _used_embedded_fonts(
    template: InvoiceTemplate, embedded_fonts: dict[str, EmbeddedFont]
) -> dict[str, EmbeddedFont]:
    """The subset of `embedded_fonts` this document's typography actually
    uses - what `render_pdf` needs to build its font objects, and no more
    (embedding a font nothing on the page draws with would be exactly the
    "embed the whole catalogue every time" `api.invoicing.truetype`'s
    docstring argues against, one level up).
    """
    return {
        code: font
        for code, font in embedded_fonts.items()
        if code in _template_font_codes(template)
    }


def _template_font_codes(template: InvoiceTemplate) -> frozenset[str]:
    return frozenset(
        {
            template.typography.heading_font,
            template.typography.body_font,
            template.typography.figures_font,
        }
    )


def _font_for(*, code: str, weight: FontWeight, embedded_fonts: dict[str, EmbeddedFont]) -> str:
    """A PDF font resource name for `code` - the embedded font if one is
    configured for it, else the base-14 face `weight` maps to.

    Never pretends: a `template_font` code with no matching entry in
    `embedded_fonts` (which is every code, until a deployment supplies font
    binaries - see `TemplatedPdfRenderer`'s docstring) silently falls back
    here, and this function is the ONE place that decision is made, so it can
    be named in one comment rather than re-justified at every call site.
    """
    embedded = embedded_fonts.get(code)
    if embedded is not None:
        return embedded.resource_name.decode("ascii")
    return Font.BOLD if weight is FontWeight.BOLD else Font.REGULAR


def _resolve_merge_tags(text: str, values: dict[str, str]) -> str:
    """FR-TPL-007's merge tags, filled from `values` - built by `_merge_values`
    from the SAME `InvoiceView`/`SupplierDetails` every other figure on the
    page comes from. An unknown tag is left as literal text (`{{typo}}`
    visibly wrong) rather than silently dropped, which is what makes a typo in
    a template's free text something the author notices.
    """

    def replace(match: re.Match[str]) -> str:
        return values.get(match[1], match[0])

    return _MERGE_TAG.sub(replace, text)


def _merge_values(view: InvoiceView, supplier: SupplierDetails, locale: str) -> dict[str, str]:
    invoice = view.invoice
    return {
        "customer_name": invoice.customer_name,
        "invoice_number": invoice.invoice_reference or "",
        "due_date": format_date(invoice.due_date, locale) if invoice.due_date else "",
        "amount": format_money(view.gross, locale),
        "supplier_vat_number": supplier.vat_number or "",
        "supplier_kvk_number": supplier.kvk_number or "",
    }


def _column_edges(visible: tuple[LineColumn, ...], right: float) -> dict[LineColumn, float]:
    """The right edge of each visible toggleable column (FR-TPL-006), evenly
    spaced leftward from `right` in the template's own chosen order - the
    LAST visible column sits at `right`, the first sits closest to the
    description column. `right` is FR-TPL-010's page-size/margin-dependent
    right edge (`_lay_out`'s own `_RIGHT`), not the module-level default.
    """
    return {column: right - index * _COLUMN_SLOT for index, column in enumerate(reversed(visible))}


def _trimmed(value: Decimal, minimum: int = 2) -> tuple[Decimal, int]:
    """`value` and the number of places to show it at: its own, but never fewer than `minimum`.

    The database stores quantities, prices and rates at three or four places (`10.0000`,
    `21.000`), and the formatter refuses to show a value at fewer places than it carries,
    because rounding would print a figure different from the stored one. So a stored `10.0000`
    is shown as `10,00` (its trailing zeros carry nothing), and `0.0350` as `0,035` (its
    third place does). Nothing is ever rounded.
    """
    exponent = value.normalize().as_tuple().exponent
    own = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    scale = max(minimum, own)
    return value.quantize(Decimal(1).scaleb(-scale)), scale


def _number(value: Decimal, locale: str) -> str:
    trimmed, scale = _trimmed(value)
    return format_number(trimmed, locale, scale=scale)


def _price(value: Decimal, locale: str) -> str:
    """A unit price with its currency symbol, at its own scale (a price may be `0,035`)."""
    trimmed, scale = _trimmed(value)
    if scale == 2:
        return format_money(trimmed, locale)
    spec = locale_spec(locale)
    digits = format_number(trimmed, locale, scale=scale)
    if spec.currency_symbol_first:
        return spec.currency_symbol + spec.currency_space + digits
    return digits + spec.currency_space + spec.currency_symbol


def _column_value(
    column: LineColumn,
    line: InvoiceLine,
    *,
    locale: str,
    rate_by_treatment: dict[str, Decimal | None],
) -> str:
    if column is LineColumn.QUANTITY:
        return _number(line.quantity, locale)
    if column is LineColumn.UNIT:
        # ADR-041's known gap, carried forward unchanged: `sales_invoice_line`
        # has no unit column (hours, pcs, kg) to print. An em dash says
        # plainly "nothing to show" rather than a blank cell that looks like
        # a rendering bug.
        return "—"
    if column is LineColumn.UNIT_PRICE:
        return _price(line.unit_price, locale)
    if column is LineColumn.DISCOUNT:
        return f"{_number(line.discount_percent, locale)}%" if line.discount_percent else ""
    if column is LineColumn.VAT_RATE:
        rate = rate_by_treatment.get(line.vat_treatment)
        return f"{_number(rate, locale)}%" if rate is not None else "—"
    if column is LineColumn.LINE_TOTAL:
        return format_money(line.line_net, locale)
    raise AssertionError(column)  # pragma: no cover - LineColumn is exhaustive above


def _lay_out(
    view: InvoiceView,
    supplier: SupplierDetails,
    language: Language,
    locale: str,
    *,
    template: InvoiceTemplate,
    document_type: DocumentType,
    logo_image: Image | None,
    embedded_fonts: dict[str, EmbeddedFont],
) -> list[Page]:
    invoice = view.invoice
    accent = Color.from_hex(template.colors.accent)
    text_color = Color.from_hex(template.colors.text)
    heading_font = _font_for(
        code=template.typography.heading_font, weight=FontWeight.BOLD, embedded_fonts=embedded_fonts
    )
    body_font = _font_for(
        code=template.typography.body_font,
        weight=template.typography.font_weight,
        embedded_fonts=embedded_fonts,
    )
    merge_values = _merge_values(view, supplier, locale)

    # FR-TPL-003: every size/spacing decision this function makes is read
    # ONCE, here, from `template.typography` - the three lookup tables above
    # plus one multiplier, never recomputed per line. `medium`/`normal`
    # (every field's default) resolve to exactly `_BODY`/`_SMALL`/`_HEADING`/
    # `_LINE_HEIGHT`, which is what keeps an unconfigured template's output
    # unchanged by this block's existence.
    body_size = _TYPE_SCALE_BODY[template.typography.type_scale]
    small_size = _TYPE_SCALE_SMALL[template.typography.type_scale]
    heading_size = _TYPE_SCALE_HEADING[template.typography.type_scale]
    letter_spacing = _LETTER_SPACING_TC[template.typography.letter_spacing]

    # FR-TPL-005/FR-TPL-010: page size, margins and the chosen layout's own
    # visual character, read ONCE here and shadowing this module's
    # module-level `_MARGIN`/`_RIGHT`/`_PAGE_BOTTOM` for the REST of this
    # function (and the `table_header`/`footer` closures defined below, which
    # close over these local names) - every reference to those three names
    # further down already means "this template's own geometry", not the
    # A4/56pt default, without having to rewrite each call site individually.
    # `PageSize.A4` + `Margins.NORMAL` (both defaults) resolve to exactly
    # 595 / 539 / 760 - today's pre-existing constants.
    page_width, page_height = _PAGE_DIMENSIONS[template.page_size]
    _MARGIN = _MARGIN_BY_STEP[template.margins]  # noqa: N806 - shadowed on purpose, see above
    _RIGHT = page_width - _MARGIN  # noqa: N806
    _PAGE_BOTTOM = page_height - _MARGIN - 26.0  # noqa: N806
    character = _LAYOUT_CHARACTER[template.layout]
    line_height = (
        _LINE_HEIGHT
        * _LINE_HEIGHT_MULTIPLIER[template.typography.line_height]
        * character.row_spacing_multiplier
    )

    def money(amount: Decimal) -> str:
        return format_money(amount, locale)

    def block(kind: ContentBlock) -> str:
        block_text = template.block_text(kind)
        if block_text is None:
            return ""
        return _resolve_merge_tags(block_text.text_for(language.value), merge_values).strip()

    # `default_letter_spacing` (api.invoicing.pdf.Page) is set once per PAGE
    # rather than passed to every `page.at(...)` call below - every new
    # `Page()` this function constructs (there are several, one per page
    # break) carries the SAME template-wide value.
    page = Page(
        default_letter_spacing=letter_spacing, page_width=page_width, page_height=page_height
    )
    pages = [page]

    # -- background band (FR-TPL-004's paper colour), if not plain white ----
    # Drawing white-on-white is indistinguishable from not drawing at all and
    # would cost every existing (uncustomised) document a rectangle object for
    # nothing; a non-white choice is a deliberate authoring decision and is
    # drawn for real.
    if template.colors.background.lower() != _WHITE:
        page.fill_rect(
            x=0,
            top=0,
            width=page_width,
            height=150.0,
            color=Color.from_hex(template.colors.background),
        )

    top = _MARGIN

    def centered(
        value: str,
        top_: float,
        *,
        font: str,
        size: float,
        color: Color | None,
        tag: StructureTag = StructureTag.PARAGRAPH,
    ) -> None:
        """`HeaderArrangement.CENTERED`'s own placement: `value` centred
        between the page's two margins (not merely the two content margins -
        FR-TPL-005 calls for a centred HEADER, and a page-wide centre is what
        actually looks centred regardless of which margin step is chosen).
        """
        page.at(
            x=(page_width - text_width(value, size)) / 2.0,
            top=top_,
            value=value,
            font=font,
            size=size,
            color=color,
            tag=tag,
        )

    title = _title(document_type, language)
    reference = invoice.invoice_reference or ""

    # FR-TPL-005's header arrangement (independent of `layout`/`character`
    # above - see `HeaderArrangement`'s docstring). Each branch below draws
    # the logo, the supplier's own "from" address, the title/reference and
    # the invoice metadata (date, due date, customer VAT number), and leaves
    # `row` at the point where the CUSTOMER ("bill to") block - unaffected by
    # this choice - should begin. `SPLIT` is today's only pre-existing
    # geometry and must reproduce it exactly.
    meta: list[tuple[str, str]] = [
        (_t("invoice.pdf.invoice_date", language), format_date(invoice.invoice_date, locale))
    ]
    if invoice.supply_date:
        meta.append(
            (_t("invoice.pdf.supply_date", language), format_date(invoice.supply_date, locale))
        )
    if invoice.due_date:
        meta.append((_t("invoice.pdf.due_date", language), format_date(invoice.due_date, locale)))
    if invoice.customer_vat_number:
        # Statutory on a reverse-charge or intra-Community invoice
        # (art. 226(4)), and harmless on any other.
        meta.append((_t("invoice.pdf.customer_vat_number", language), invoice.customer_vat_number))

    #: Set only by the `SPLIT` branch below - see its own comment on why the
    #: metadata column is drawn LATER, at its original position, rather than
    #: here alongside the rest of the header.
    meta_deferred_top: float | None = None

    if template.header_arrangement is HeaderArrangement.SPLIT:
        # -- logo (FR-TPL-001), stacked above the supplier block regardless of
        # its horizontal position - see `TemplatedPdfRenderer`'s docstring on
        # why reflowing text around a side-by-side logo is out of scope here.
        if logo_image is not None:
            height = _LOGO_HEIGHT[template.logo.size]
            width, height = logo_image.fit(max_width=200.0, max_height=height)
            if template.logo.position is LogoPosition.LEFT:
                x = _MARGIN
            elif template.logo.position is LogoPosition.RIGHT:
                x = _RIGHT - width
            else:
                x = (page_width - width) / 2.0
            page.place_image(
                image=logo_image,
                x=x,
                top=top,
                width=width,
                height=height,
                # FR-TPL-016: the business's own name, never the literal word
                # "logo" (`Placement.__post_init__` only refuses BLANK alt
                # text, so this caller carries the real obligation).
                alt=supplier.legal_name or _t("invoice.pdf.invoice_title", language),
            )
            top += height + 10.0

        # -- supplier block, top left ---------------------------------------
        supplier_top = top
        page.at(
            x=_MARGIN,
            top=supplier_top,
            value=supplier.legal_name or "",
            font=heading_font,
            size=body_size,
            color=text_color,
        )
        supplier_top += line_height
        for part in (supplier.address_line1, supplier.address_line2, _postcode_city(supplier)):
            if part:
                page.at(
                    x=_MARGIN,
                    top=supplier_top,
                    value=part,
                    size=small_size,
                    font=body_font,
                    color=text_color,
                )
                supplier_top += line_height

        # -- title and reference, top right ----------------------------------
        page.at(
            x=_RIGHT - text_width(title, heading_size),
            top=top + 4,
            value=title,
            font=heading_font,
            size=heading_size,
            color=accent,
            # FR-TPL-016: the one heading on the page.
            tag=StructureTag.HEADING,
        )
        if reference:
            page.at(
                x=_RIGHT - text_width(reference, body_size),
                top=top + 26,
                value=reference,
                font=heading_font,
                size=body_size,
                color=text_color,
            )

        # Metadata is drawn LATER, at its original position after the
        # customer ("bill to") block - not here - so the SPLIT default keeps
        # drawing every mark in EXACTLY the same order it always has (the
        # byte-identity guarantee: reordering these `page.at` calls would
        # reorder both the content stream and the MCIDs `_build_structure`
        # assigns from it, even though nothing would look different). See
        # `meta_deferred_top` below, used right after the customer block.
        meta_deferred_top = top + 78
        row = max(supplier_top, top + 60) + 18

    elif template.header_arrangement is HeaderArrangement.STACKED:
        # One column: logo centred, title below it, supplier address below
        # that, metadata below that - all left-aligned at the margin except
        # the logo itself.
        if logo_image is not None:
            height = _LOGO_HEIGHT[template.logo.size]
            width, height = logo_image.fit(max_width=200.0, max_height=height)
            page.place_image(
                image=logo_image,
                x=(page_width - width) / 2.0,
                top=top,
                width=width,
                height=height,
                alt=supplier.legal_name or _t("invoice.pdf.invoice_title", language),
            )
            top += height + 10.0

        page.at(
            x=_MARGIN,
            top=top,
            value=title,
            font=heading_font,
            size=heading_size,
            color=accent,
            tag=StructureTag.HEADING,
        )
        top += heading_size + 8.0
        if reference:
            page.at(
                x=_MARGIN,
                top=top,
                value=reference,
                font=heading_font,
                size=body_size,
                color=text_color,
            )
            top += line_height

        page.at(
            x=_MARGIN,
            top=top,
            value=supplier.legal_name or "",
            font=heading_font,
            size=body_size,
            color=text_color,
        )
        top += line_height
        for part in (supplier.address_line1, supplier.address_line2, _postcode_city(supplier)):
            if part:
                page.at(
                    x=_MARGIN,
                    top=top,
                    value=part,
                    size=small_size,
                    font=body_font,
                    color=text_color,
                )
                top += line_height

        for label, value in meta:
            page.at(
                x=_MARGIN,
                top=top,
                value=f"{label} {value}",
                size=small_size,
                font=body_font,
                color=text_color,
            )
            top += line_height

        row = top + 16

    else:
        assert template.header_arrangement is HeaderArrangement.CENTERED
        # Everything centred: logo, then the supplier address below it, then
        # (further below - the extra gap the enum's own docstring calls for)
        # the title/reference/metadata, all centred too.
        if logo_image is not None:
            height = _LOGO_HEIGHT[template.logo.size]
            width, height = logo_image.fit(max_width=200.0, max_height=height)
            page.place_image(
                image=logo_image,
                x=(page_width - width) / 2.0,
                top=top,
                width=width,
                height=height,
                alt=supplier.legal_name or _t("invoice.pdf.invoice_title", language),
            )
            top += height + 10.0

        centered(
            supplier.legal_name or "", top, font=heading_font, size=body_size, color=text_color
        )
        top += line_height
        for part in (supplier.address_line1, supplier.address_line2, _postcode_city(supplier)):
            if part:
                centered(part, top, font=body_font, size=small_size, color=text_color)
                top += line_height

        top += 16.0
        centered(
            title, top, font=heading_font, size=heading_size, color=accent, tag=StructureTag.HEADING
        )
        top += heading_size + 8.0
        if reference:
            centered(reference, top, font=heading_font, size=body_size, color=text_color)
            top += line_height

        for label, value in meta:
            centered(f"{label} {value}", top, font=body_font, size=small_size, color=text_color)
            top += line_height

        row = top + 16

    # -- FR-TPL-005's MODERN layout: a tinted band behind the whole header
    # just drawn, sized to it rather than a fixed guess - see `_tint`.
    if character.uses_background_band:
        page.fill_rect(
            x=0, top=0, width=page_width, height=row, color=_tint(accent, toward_white=0.9)
        )

    # -- HEADER content block (FR-TPL-007), full width -----------------------
    header_text = block(ContentBlock.HEADER)
    if header_text:
        for fragment in wrap(header_text, width=_RIGHT - _MARGIN, size=small_size):
            page.at(
                x=_MARGIN,
                top=row,
                value=fragment,
                size=small_size,
                font=body_font,
                color=text_color,
            )
            row += line_height
        row += 6

    # -- customer block ----------------------------------------------------
    page.at(
        x=_MARGIN,
        top=row,
        value=_t("invoice.pdf.bill_to", language),
        size=small_size,
        font=body_font,
        color=text_color,
    )
    row += line_height
    page.at(
        x=_MARGIN,
        top=row,
        value=invoice.customer_name,
        font=heading_font,
        size=body_size,
        color=text_color,
    )
    row += line_height
    # The address is the snapshot 0037 froze, already rendered as a block by
    # `api.customers.address.format_address`. Split rather than re-derived: the
    # document says what it says.
    for part in invoice.customer_address.splitlines():
        if part.strip():
            page.at(
                x=_MARGIN,
                top=row,
                value=part.strip(),
                size=small_size,
                font=body_font,
                color=text_color,
            )
            row += line_height

    # -- document metadata, right column (HeaderArrangement.SPLIT only - the
    # other two arrangements already drew their metadata inline, above,
    # single-column) - kept at this EXACT position in the draw order for
    # byte-identity with pre-existing output. -------------------------------
    if meta_deferred_top is not None:
        meta_top = meta_deferred_top
        for label, value in meta:
            page.at(
                x=_RIGHT - 200.0,
                top=meta_top,
                value=label,
                size=small_size,
                font=body_font,
                color=text_color,
            )
            page.at(
                x=_RIGHT - text_width(value, small_size),
                top=meta_top,
                value=value,
                size=small_size,
                font=body_font,
                color=text_color,
            )
            meta_top += line_height
        row = max(row, meta_top) + 16

    # -- INTRO content block (FR-TPL-007) -----------------------------------
    intro_text = block(ContentBlock.INTRO)
    if intro_text:
        for fragment in wrap(intro_text, width=_RIGHT - _MARGIN, size=small_size):
            page.at(
                x=_MARGIN,
                top=row,
                value=fragment,
                size=small_size,
                font=body_font,
                color=text_color,
            )
            row += line_height
        row += 6

    # -- line table (FR-TPL-006) --------------------------------------------
    visible = template.columns.visible_in_order
    edges = _column_edges(visible, _RIGHT)
    desc_width = max(
        120.0, (edges[visible[0]] - _COLUMN_SLOT if visible else _RIGHT) - _MARGIN - 10.0
    )
    rate_by_treatment = {group.treatment: group.rate for group in view.groups}

    def table_header(top_: float) -> float:
        """FR-TPL-016's `TH` row - see `_lay_out`'s old single-layout
        ancestor's docstring on why this is its own row 0 on every page.
        """
        page.at(
            x=_MARGIN,
            top=top_,
            value=_t("invoice.pdf.column_description", language),
            font=heading_font,
            size=small_size,
            color=text_color,
            tag=StructureTag.TABLE_HEADER,
            row=0,
        )
        for column in visible:
            label = _t(_COLUMN_LABEL_KEYS[column], language)
            page.at(
                x=edges[column] - text_width(label, small_size),
                top=top_,
                value=label,
                font=heading_font,
                size=small_size,
                color=text_color,
                tag=StructureTag.TABLE_HEADER,
                row=0,
            )
        if character.draws_rules:
            page.rule(x1=_MARGIN, top=top_ + 4, x2=_RIGHT, color=accent)
        return top_ + line_height + 4

    def footer() -> None:
        """The `legal_identity` block, on every page - art. 226(3)'s VAT
        number and the Handelsregisterwet's KvK number are both already
        locked into this block's text by FR-TPL-009, so printing the block IS
        printing the statutory identifiers, with whatever surrounding prose
        the template's author chose.

        `is_artifact=True` for the same load-bearing reason the single-layout
        ancestor of this function gave: this runs on the page ABOUT to be
        replaced, between its last line item and the next page's repeated
        table header, and ordinary paragraph content there would end the
        line-item Table at every page break.
        """
        text = block(ContentBlock.LEGAL_IDENTITY)
        if text:
            page.at(
                x=_MARGIN,
                top=_PAGE_BOTTOM + 30,
                value=text,
                size=small_size,
                font=body_font,
                color=text_color,
                is_artifact=True,
            )

    row = table_header(row)
    # FR-TPL-010's carried-forward subtotal: the running net total of every
    # line drawn so far (this page and every prior one). Only ever printed at
    # an ACTUAL page break below - a single-page invoice never touches this,
    # which is what keeps it byte-identical to before this feature existed.
    running_net = Decimal("0.00")
    for line in invoice.lines:
        wrapped = wrap(line.description, width=desc_width, size=small_size)
        needed = line_height * len(wrapped)

        if row + needed > _PAGE_BOTTOM:
            # "Subtotal carried forward", right-aligned below the last line
            # item drawn on the page about to be replaced - BEFORE `footer()`,
            # per FR-TPL-010. `is_artifact=True` for the identical reason
            # `footer()` itself uses it: this sits between the last line item
            # and the next page's repeated table header, and ordinary
            # paragraph content there would end the line-item Table at this
            # page break (see `_build_structure`'s docstring in api.invoicing.
            # pdf).
            _right(
                page,
                _RIGHT,
                row,
                _t("invoice.pdf.carried_forward", language, amount=money(running_net)),
                small_size,
                font=body_font,
                color=text_color,
                is_artifact=True,
            )
            footer()
            page = Page(
                default_letter_spacing=letter_spacing,
                page_width=page_width,
                page_height=page_height,
            )
            pages.append(page)
            row = _MARGIN
            row = table_header(row)
            # "Balance brought forward", immediately after the repeated table
            # header and before the first line item resumes - the SAME
            # running total the completing page just carried forward, so a
            # reader can tie the two figures together across the page break.
            _right(
                page,
                _RIGHT,
                row,
                _t("invoice.pdf.brought_forward", language, amount=money(running_net)),
                small_size,
                font=body_font,
                color=text_color,
                is_artifact=True,
            )
            row += line_height

        # FR-TPL-016: every fragment of this line's description is ONE cell in
        # the structure tree, grouped by `line.position` (positions start at
        # 1, so they never collide with the header row's row=0).
        for offset, fragment in enumerate(wrapped):
            page.at(
                x=_MARGIN,
                top=row + offset * line_height,
                value=fragment,
                size=small_size,
                font=body_font,
                color=text_color,
                tag=StructureTag.TABLE_CELL,
                row=line.position,
            )
        for column in visible:
            value = _column_value(column, line, locale=locale, rate_by_treatment=rate_by_treatment)
            _right(
                page,
                edges[column],
                row,
                value,
                small_size,
                font=body_font,
                color=text_color,
                tag=StructureTag.TABLE_CELL,
                row_=line.position,
            )
        row += needed
        running_net += line.line_net

    # -- totals ------------------------------------------------------------
    if row + 140 > _PAGE_BOTTOM:
        footer()
        page = Page(
            default_letter_spacing=letter_spacing, page_width=page_width, page_height=page_height
        )
        pages.append(page)
        row = _MARGIN

    if character.draws_rules:
        page.rule(x1=_MARGIN, top=row + 2, x2=_RIGHT, color=accent)
    row += 12

    # FR-AR-002 / Art. 226(8)(10): the taxable amount and the VAT PER RATE.
    # Always left-aligned label / right-aligned amount under the line-item
    # table, regardless of `totals_position` - only the SUMMARY block below
    # (subtotal/VAT total/total) is what FR-TPL-005 calls "the totals block".
    for group in view.groups:
        rate_text = "-" if group.rate is None else f"{_number(group.rate, locale)}%"
        label = _t("invoice.pdf.vat_group", language, rate=rate_text, base=money(group.taxable))
        page.at(x=_MARGIN, top=row, value=label, size=small_size, font=body_font, color=text_color)
        _right(page, _RIGHT, row, money(group.vat), small_size, font=body_font, color=text_color)
        row += line_height

    row += 4

    # FR-TPL-005's totals block position. `RIGHT` (the default) reproduces
    # today's pre-existing geometry exactly: label at a fixed 280pt column
    # ending at the right margin, amount right-aligned at the right margin
    # itself. `LEFT` moves the whole line (label AND amount, as one string)
    # to the page margin. `FULL_WIDTH` draws a light band behind the same
    # RIGHT-shaped two-part row, spanning margin to margin.
    if template.totals_position is TotalsPosition.FULL_WIDTH:
        band_height = 3 * (line_height + 3) + 6
        page.fill_rect(
            x=_MARGIN,
            top=row - 4,
            width=_RIGHT - _MARGIN,
            height=band_height,
            color=_tint(accent, toward_white=0.9),
        )

    for key, amount, bold in (
        ("invoice.pdf.subtotal", view.net, False),
        ("invoice.pdf.vat_total", view.vat, False),
        ("invoice.pdf.total", view.gross, True),
    ):
        font = heading_font if bold else body_font
        color = accent if bold else text_color
        amount_text = money(amount)
        if template.totals_position is TotalsPosition.LEFT:
            page.at(
                x=_MARGIN,
                top=row,
                value=f"{_t(key, language)}  {amount_text}",
                font=font,
                size=body_size,
                color=color,
            )
        else:
            label_x = (
                _MARGIN if template.totals_position is TotalsPosition.FULL_WIDTH else _RIGHT - 280.0
            )
            page.at(
                x=label_x,
                top=row,
                value=_t(key, language),
                font=font,
                size=body_size,
                color=color,
            )
            _right(page, _RIGHT, row, amount_text, body_size, font=font, color=color)
        row += line_height + (3 if bold else 0)

    # -- SI-02: EPC069-12 "pay by bank" QR code -------------------------------
    # One injection point regardless of header_arrangement/totals_position -
    # both vary WHERE the totals block sits, not what comes after it. Skipped
    # entirely (not a blank square) when there is nothing to pay TO
    # (no IBAN on file) or nothing owed BY the customer (a credit note is
    # money going back, not a request for one - the same reasoning
    # api.invoicing.delivery's covering-email body omits a due date for one).
    if supplier.iban and not invoice.is_credit_note:
        qr_size = 90.0
        row += 12
        if row + qr_size > _PAGE_BOTTOM:
            footer()
            page = Page(
                default_letter_spacing=letter_spacing,
                page_width=page_width,
                page_height=page_height,
            )
            pages.append(page)
            row = _MARGIN
        try:
            qr_png = render_epc_qr(
                EpcQrRequest(
                    beneficiary_name=supplier.legal_name or "",
                    iban=supplier.iban,
                    amount=view.gross,
                    reference=invoice.invoice_reference or "",
                )
            )
            page.place_image(
                image=load_image(qr_png),
                x=_MARGIN,
                top=row,
                width=qr_size,
                height=qr_size,
                alt=_t("invoice.pdf.qr_alt", language),
            )
            page.at(
                x=_MARGIN + qr_size + 10,
                top=row + qr_size / 2 - small_size,
                value=_t("invoice.pdf.qr_caption", language),
                size=small_size,
                font=body_font,
                color=text_color,
            )
        except ValueError:
            # A malformed IBAN that somehow reached this column (SI-02's
            # write-time check in api.onboarding.routes is what should
            # normally prevent this) is not a reason to refuse rendering the
            # rest of a real invoice - it just means no QR code this time,
            # exactly like an administration with none on file at all.
            pass
        else:
            row += qr_size

    # -- PAYMENT_TERMS content block (FR-TPL-007) ----------------------------
    payment_terms_text = block(ContentBlock.PAYMENT_TERMS)
    if payment_terms_text:
        row += 10
        for fragment in wrap(payment_terms_text, width=_RIGHT - _MARGIN, size=small_size):
            if row > _PAGE_BOTTOM:
                footer()
                page = Page(
                    default_letter_spacing=letter_spacing,
                    page_width=page_width,
                    page_height=page_height,
                )
                pages.append(page)
                row = _MARGIN
            page.at(
                x=_MARGIN,
                top=row,
                value=fragment,
                size=small_size,
                font=body_font,
                color=text_color,
            )
            row += line_height

    # -- legal wording -------------------------------------------------------
    # Art. 226(11). Content, not a footnote: see the module docstring.
    statements = [text for group in view.groups if (text := view.wording.get(group.treatment))]
    if statements:
        row += 10
        for statement in statements:
            for fragment in wrap(statement, width=_RIGHT - _MARGIN, size=small_size):
                if row > _PAGE_BOTTOM:
                    footer()
                    page = Page(
                        default_letter_spacing=letter_spacing,
                        page_width=page_width,
                        page_height=page_height,
                    )
                    pages.append(page)
                    row = _MARGIN
                page.at(
                    x=_MARGIN,
                    top=row,
                    value=fragment,
                    size=small_size,
                    font=body_font,
                    color=text_color,
                )
                row += line_height
            row += 3

    if invoice.notes:
        row += 8
        for fragment in wrap(invoice.notes, width=_RIGHT - _MARGIN, size=small_size):
            if row > _PAGE_BOTTOM:
                footer()
                page = Page(
                    default_letter_spacing=letter_spacing,
                    page_width=page_width,
                    page_height=page_height,
                )
                pages.append(page)
                row = _MARGIN
            page.at(
                x=_MARGIN,
                top=row,
                value=fragment,
                size=small_size,
                font=body_font,
                color=text_color,
            )
            row += line_height

    # -- FOOTER content block (FR-TPL-007), once, at the end of the document -
    footer_text = block(ContentBlock.FOOTER)
    if footer_text:
        row += 10
        for fragment in wrap(footer_text, width=_RIGHT - _MARGIN, size=small_size):
            if row > _PAGE_BOTTOM:
                footer()
                page = Page(
                    default_letter_spacing=letter_spacing,
                    page_width=page_width,
                    page_height=page_height,
                )
                pages.append(page)
                row = _MARGIN
            page.at(
                x=_MARGIN,
                top=row,
                value=fragment,
                size=small_size,
                font=body_font,
                color=text_color,
            )
            row += line_height

    footer()
    _paginate(pages, language, _RIGHT, _PAGE_BOTTOM)
    return pages


def _right(
    page: Page,
    column: float,
    top: float,
    value: str,
    size: float,
    *,
    font: str = Font.REGULAR,
    color: Color | None = None,
    tag: StructureTag = StructureTag.PARAGRAPH,
    row_: int | None = None,
    is_artifact: bool = False,
) -> None:
    page.at(
        x=column - text_width(value, size),
        top=top,
        value=value,
        font=font,
        color=color,
        size=size,
        tag=tag,
        row=row_,
        is_artifact=is_artifact,
    )


def _postcode_city(supplier: SupplierDetails) -> str | None:
    parts = [part for part in (supplier.postal_code, supplier.city) if part]
    return "  ".join(parts) if parts else None


def _paginate(pages: list[Page], language: Language, right: float, page_bottom: float) -> None:
    """FR-TPL-010's page numbering, added once the total is known.

    Written after layout rather than during it, because "page 1 of 3" cannot be
    drawn until the third page exists - the reason a single-pass renderer
    usually ends up saying "page 1 of ?".

    `right`/`page_bottom` are `_lay_out`'s own page-size/margin-dependent
    values, not the module-level defaults - a Letter-sized or wide-margin
    template's page number belongs at ITS right edge and bottom, not A4's.
    """
    total = len(pages)
    for index, page in enumerate(pages, start=1):
        label = _t("invoice.pdf.page", language, page=index, pages=total)
        page.at(
            x=right - text_width(label, _SMALL),
            top=page_bottom + 30,
            value=label,
            size=_SMALL,
            is_artifact=True,
        )
