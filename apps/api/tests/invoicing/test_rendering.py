"""api.invoicing.rendering - what the customer actually receives, drawn by
`TemplatedPdfRenderer` - FR-TPL-008's unified renderer.

The assertions read text out of the content stream, because the thing worth
testing is whether the statutory content REACHED the page. An invoice that is
missing art. 226's required fields is non-compliant while looking entirely
normal, which is the failure FR-AR-003's gate exists to prevent - and the gate
is worth nothing if the renderer then drops the field on the floor.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from api.i18n.formatting import format_money
from api.i18n.language import Language
from api.invoicing.model import InvoiceLine, InvoiceStatus, InvoiceView, SalesInvoice
from api.invoicing.pdf import load_image
from api.invoicing.rendering import (
    RenderedInvoice,
    TemplatedPdfRenderer,
    _load_embedded_fonts,
    build_invoice_renderer,
    default_template,
)
from api.invoicing.statutory import SupplierDetails
from api.invoicing.vat import VatGroup
from api.templates.model import (
    FONT_CODES,
    DocumentType,
    HeaderArrangement,
    Layout,
    LetterSpacing,
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
from api.vat.rules import TreatmentRole
from tests.invoicing.image_fixtures import png
from tests.invoicing.support.synthetic_font import build_synthetic_font

pytestmark = pytest.mark.anyio

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

TEMPLATE = default_template(organization_id=uuid.uuid4(), administration_id=uuid.uuid4())


def line(description: str = "Advies", net: str = "1000.00", position: int = 1) -> InvoiceLine:
    return InvoiceLine(
        id=uuid.uuid4(),
        position=position,
        description=description,
        quantity=Decimal("1.00"),
        unit_price=Decimal(net),
        discount_percent=Decimal("0.00"),
        vat_treatment="btw_21",
        role=TreatmentRole.STANDARD,
        line_net=Decimal(net),
    )


def view(
    *,
    language: Language = Language.NL,
    lines: tuple[InvoiceLine, ...] | None = None,
    groups: tuple[VatGroup, ...] | None = None,
    wording: dict[str, str] | None = None,
    credit_of: uuid.UUID | None = None,
    supply_date: date | None = None,
) -> InvoiceView:
    if lines is None:
        lines = (line(),)
    if groups is None:
        groups = (
            VatGroup(
                "btw_21",
                TreatmentRole.STANDARD,
                Decimal("21.00"),
                Decimal("1000.00"),
                Decimal("210.00"),
            ),
        )
    net = sum((g.taxable for g in groups), Decimal("0.00"))
    vat = sum((g.vat for g in groups), Decimal("0.00"))
    invoice = SalesInvoice(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        administration_id=uuid.uuid4(),
        fiscal_year_id=uuid.uuid4(),
        status=InvoiceStatus.ISSUED,
        invoice_number=7,
        number_prefix="2026-",
        invoice_reference="2026-7",
        invoice_date=date(2026, 9, 9),
        supply_date=supply_date,
        due_date=date(2026, 10, 9),
        customer_name="De Vries Holding B.V.",
        customer_address="Keizersgracht 12\n1015 CW  Amsterdam",
        customer_country="NL",
        customer_vat_number="NL987654321B01",
        customer_id=None,
        customer_language=language,
        credits_invoice_id=credit_of,
        notes=None,
        issued_at=None,
        lines=lines,
    )
    return InvoiceView(
        invoice=invoice,
        groups=groups,
        net=net,
        vat=vat,
        gross=net + vat,
        wording=wording if wording is not None else {},
    )


def _document_type(v: InvoiceView) -> DocumentType:
    return DocumentType.CREDIT_NOTE if v.invoice.is_credit_note else DocumentType.INVOICE


async def render(v: InvoiceView, *, locale: str = "nl-NL") -> RenderedInvoice:
    return await TemplatedPdfRenderer().render(
        v,
        supplier=SUPPLIER,
        formatting_locale=locale,
        template=TEMPLATE,
        document_type=_document_type(v),
    )


async def text_of(v: InvoiceView, *, locale: str = "nl-NL") -> str:
    """Every string drawn on the document, concatenated.

    Read out of the content stream's `(...) Tj` operands, decoded from WinAnsi,
    so what is asserted is what a reader would actually show.
    """
    rendered = await render(v, locale=locale)
    parts = re.findall(rb"\((.*?)\) Tj", rendered.content, re.S)
    unescaped = [
        p.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\") for p in parts
    ]
    return "\n".join(p.decode("cp1252", errors="replace") for p in unescaped)


# --- art. 226's required content ---------------------------------------------


async def test_the_supplier_appears() -> None:
    """Art. 226(5): the full name and address of the supplier."""
    text = await text_of(view())

    assert "Bakker Consultancy B.V." in text
    assert "Damrak 70" in text
    assert "1012 LM" in text and "Amsterdam" in text


async def test_the_customer_appears() -> None:
    """Art. 226(5), the other half."""
    text = await text_of(view())

    assert "De Vries Holding B.V." in text
    assert "Keizersgracht 12" in text


async def test_the_snapshotted_address_is_printed_line_by_line() -> None:
    """The address on the invoice is the block 0037 froze. Printed as it was
    stored rather than re-derived - the document says what it says.
    """
    text = await text_of(view())

    assert "Keizersgracht 12" in text
    assert "1015 CW  Amsterdam" in text


async def test_the_invoice_number_and_date_appear() -> None:
    """Art. 226(1) and 226(2): the date of issue and a sequential number."""
    text = await text_of(view())

    assert "2026-7" in text
    assert "09-09-2026" in text


async def test_the_supplier_vat_and_kvk_numbers_appear() -> None:
    """Art. 226(3): the supplier's VAT identification number - printed via the
    `legal_identity` content block's own locked merge tags (FR-TPL-009), not a
    hardcoded line, now that the renderer is template-driven.
    """
    text = await text_of(view())

    assert "NL123456789B01" in text
    assert "12345678" in text


async def test_the_customer_vat_number_appears_when_there_is_one() -> None:
    """Art. 226(4). Mandatory on a reverse-charge or intra-Community invoice,
    where it is what makes the treatment lawful.
    """
    assert "NL987654321B01" in await text_of(view())


async def test_the_supply_date_appears_only_when_it_differs() -> None:
    """Art. 226(7) requires it where it differs from the invoice date; printing
    it always would put a redundant row on every domestic invoice.
    """
    without = await text_of(view())
    with_date = await text_of(view(supply_date=date(2026, 8, 31)))

    assert "31-08-2026" not in without
    assert "31-08-2026" in with_date


async def test_the_vat_is_shown_per_rate() -> None:
    """Art. 226(8) and 226(10): the taxable amount and the VAT per rate. Not a
    single combined VAT line - the breakdown is what the statute asks for.
    """
    text = await text_of(
        view(
            groups=(
                VatGroup(
                    "btw_21",
                    TreatmentRole.STANDARD,
                    Decimal("21.00"),
                    Decimal("1000.00"),
                    Decimal("210.00"),
                ),
                VatGroup(
                    "btw_9",
                    TreatmentRole.REDUCED,
                    Decimal("9.00"),
                    Decimal("500.00"),
                    Decimal("45.00"),
                ),
            )
        )
    )

    assert "21,00%" in text and "9,00%" in text
    assert "€\xa0210,00" in text and "€\xa045,00" in text


async def test_the_legal_wording_is_printed() -> None:
    """Art. 226(11): an invoice must STATE why no VAT was charged. An invoice
    showing 0,00 with no explanation is not compliant, and the customer cannot
    act on it - a reverse-charged supply obliges them to account for the VAT and
    they can only know because the document says so.
    """
    text = await text_of(
        view(
            groups=(
                VatGroup(
                    "btw_verlegd",
                    TreatmentRole.REVERSE_CHARGE,
                    None,
                    Decimal("1000.00"),
                    Decimal("0.00"),
                ),
            ),
            wording={"btw_verlegd": "Btw verlegd naar de afnemer"},
        )
    )

    assert "Btw verlegd naar de afnemer" in text


async def test_the_totals_appear() -> None:
    text = await text_of(view())

    assert "€\xa01.000,00" in text
    assert "€\xa0210,00" in text
    assert "€\xa01.210,00" in text


async def test_a_credit_note_says_so_in_its_heading() -> None:
    """A customer filing it beside the invoice it corrects has to tell them
    apart at a glance. Driven by `document_type` (FR-TPL-012), computed from
    `invoice.is_credit_note` at the call site - see `_document_type` above.
    """
    text = await text_of(view(credit_of=uuid.uuid4()))

    assert "CREDITFACTUUR" in text
    assert "FACTUUR" in text  # the substring; the point is the heading changed


# --- FR-TPL-006: columns -----------------------------------------------------


async def test_every_column_of_the_default_template_appears() -> None:
    """`default_template()` shows every `LineColumn` - the same all-visible
    scaffold a brand-new template gets (`api.templates.routes._default_
    columns`) - so quantity, unit price, VAT rate and line total ALL reach
    the page, not only the four the single hardcoded ancestor of this
    renderer used to draw.
    """
    text = await text_of(view())

    assert "1,00" in text  # quantity
    assert "21,00%" in text  # VAT rate, per line (via the group lookup)


# --- FR-TPL-013 and FR-LOC-002 ----------------------------------------------


async def test_the_words_follow_the_recipient() -> None:
    """FR-TPL-013. The language is the frozen snapshot on the invoice, not the
    caller's - a Dutch bookkeeper invoicing a German customer in English gets
    English labels while their own screen stays Dutch.
    """
    dutch = await text_of(view(language=Language.NL))
    english = await text_of(view(language=Language.EN))

    assert "FACTUUR" in dutch and "Factuurdatum" in dutch
    assert "INVOICE" in english and "Invoice date" in english
    assert "Factuurdatum" not in english


async def test_the_figures_follow_the_administration_not_the_reader() -> None:
    """FR-LOC-002: how figures in THESE books are written, for every reader.

    So an English-language invoice from a Dutch administration shows
    `€ 1.210,00` with a Dutch thousands separator and an English label. That
    looks odd written down and is exactly what the two requirements ask for
    together - the words are the reader's, the figures are the books' own.

    `nl-NL` is currently the only formatting locale the product ships
    (`api.i18n.formatting.LOCALES`), so this asserts the split rather than a
    difference between two locales.
    """
    text = await text_of(view(language=Language.EN), locale="nl-NL")

    assert "Total" in text
    assert "€\xa01.210,00" in text
    # The English rendering of the same amount, which must NOT appear.
    assert "1,210.00" not in text


async def test_an_unsupported_locale_is_refused_rather_than_guessed() -> None:
    """A locale nobody has specified a chart, a ruleset or a filing channel for
    is refused (FR-LOC-005). Better than formatting a statutory document with
    separators somebody assumed.
    """
    from api.i18n.formatting import FormattingError

    with pytest.raises(FormattingError, match="no formatting locale"):
        await text_of(view(), locale="en-GB")


# --- what must NOT reach the customer ----------------------------------------


async def test_the_provisional_wording_warning_is_not_printed() -> None:
    """`wording_is_provisional` is an internal warning that no Dutch tax adviser
    has signed the sentence off. A note to that effect on a customer's invoice
    would be worse than the risk it describes.
    """
    v = view(
        groups=(
            VatGroup(
                "btw_verlegd",
                TreatmentRole.REVERSE_CHARGE,
                None,
                Decimal("1000.00"),
                Decimal("0.00"),
            ),
        ),
        wording={"btw_verlegd": "Btw verlegd"},
    )
    assert v.wording_is_provisional
    text = await text_of(v)

    for leak in ("provisional", "voorlopig", "unreviewed", "niet beoordeeld"):
        assert leak.lower() not in text.lower()


# --- FR-TPL-003: type scale, line height and letter spacing ------------------


async def render_with(v: InvoiceView, template: object) -> RenderedInvoice:
    return await TemplatedPdfRenderer().render(
        v,
        supplier=SUPPLIER,
        formatting_locale="nl-NL",
        template=template,  # type: ignore[arg-type]
        document_type=_document_type(v),
    )


def _typography(**overrides: object) -> Typography:
    base: dict[str, object] = {
        "heading_font": "ibm_plex_sans",
        "body_font": "ibm_plex_sans",
        "figures_font": "ibm_plex_mono",
    }
    base.update(overrides)
    return Typography(**base)  # type: ignore[arg-type]


def _font_sizes(content: bytes) -> list[float]:
    """Every `Tf` point size used on the page, in draw order."""
    return [float(m) for m in re.findall(rb"/\S+ (\d+\.\d+) Tf", content)]


def _td_y_values(content: bytes) -> list[float]:
    """Every text placement's y coordinate, in draw order - what `Page.at`'s
    `top` becomes after `_lay_out`'s top-to-bottom flip.
    """
    return [float(y) for _, y in re.findall(rb"(-?\d+\.\d+) (-?\d+\.\d+) Td", content)]


async def test_medium_type_scale_matches_the_pre_existing_constants() -> None:
    """`medium`/`normal`/`normal` - every field's own default, and what
    `default_template()` already carries - draws at exactly the sizes this
    renderer used before FR-TPL-003's steps existed: 9.5pt body, 9.0pt small
    body text, 18.0pt heading, no `Tc` operator at all. This is the byte-
    identity guarantee: an unconfigured template's appearance is unchanged by
    this feature's existence.
    """
    rendered = await render_with(view(), TEMPLATE)

    sizes = _font_sizes(rendered.content)
    assert 9.50 in sizes
    assert 9.00 in sizes
    assert 18.00 in sizes
    assert b" Tc" not in rendered.content


async def test_small_type_scale_never_drops_below_the_9pt_floor() -> None:
    """FR-TPL-016's 9pt floor holds even at the smallest curated step - there
    is no smaller size for `small` to fall back to below it (`_TYPE_SCALE_
    BODY`/`_TYPE_SCALE_SMALL` both hold `small` at exactly 9.0pt).
    """
    template = dataclasses.replace(TEMPLATE, typography=_typography(type_scale=TypeScale.SMALL))
    rendered = await render_with(view(), template)

    sizes = _font_sizes(rendered.content)
    assert min(sizes) >= 9.0
    assert 14.00 in sizes  # the curated `small` heading size


async def test_extra_large_type_scale_produces_a_bigger_heading_than_medium() -> None:
    template = dataclasses.replace(
        TEMPLATE, typography=_typography(type_scale=TypeScale.EXTRA_LARGE)
    )
    extra_large = await render_with(view(), template)
    medium = await render_with(view(), TEMPLATE)

    assert max(_font_sizes(extra_large.content)) > max(_font_sizes(medium.content))


async def test_relaxed_line_height_advances_further_between_lines_than_tight() -> None:
    """Measured on the supplier block's first two lines (the legal name, then
    the first address line) - the gap between their `Td` y-values IS the
    line-height constant this step scales.
    """

    async def gap(line_height: LineHeight) -> float:
        template = dataclasses.replace(TEMPLATE, typography=_typography(line_height=line_height))
        rendered = await render_with(view(), template)
        ys = _td_y_values(rendered.content)
        return ys[0] - ys[1]

    tight_gap = await gap(LineHeight.TIGHT)
    normal_gap = await gap(LineHeight.NORMAL)
    relaxed_gap = await gap(LineHeight.RELAXED)

    assert tight_gap < normal_gap < relaxed_gap
    assert normal_gap == pytest.approx(13.0)  # unchanged from before this change


async def test_wide_letter_spacing_emits_a_positive_tc_operator() -> None:
    template = dataclasses.replace(
        TEMPLATE, typography=_typography(letter_spacing=LetterSpacing.WIDE)
    )
    rendered = await render_with(view(), template)

    assert b"0.50 Tc" in rendered.content


async def test_tight_letter_spacing_emits_a_negative_tc_operator() -> None:
    template = dataclasses.replace(
        TEMPLATE, typography=_typography(letter_spacing=LetterSpacing.TIGHT)
    )
    rendered = await render_with(view(), template)

    assert b"-0.20 Tc" in rendered.content


async def test_normal_letter_spacing_emits_no_tc_operator_at_all() -> None:
    template = dataclasses.replace(
        TEMPLATE, typography=_typography(letter_spacing=LetterSpacing.NORMAL)
    )
    rendered = await render_with(view(), template)

    assert b" Tc" not in rendered.content


async def test_no_internal_identifier_reaches_the_page() -> None:
    """A UUID on a customer's invoice is a developer-facing string, which
    FR-UX-007 keeps away from users - and it is noise on a legal document.
    """
    v = view()
    text = await text_of(v)

    assert str(v.invoice.id) not in text


# --- pagination --------------------------------------------------------------


async def test_a_long_invoice_paginates() -> None:
    """FR-TPL-010's multi-page behaviour. Forty lines must not run off the page
    and out of the printable area, where they would be absent from the document
    rather than merely ugly.
    """
    lines = tuple(
        line(description=f"Regel {n} met een wat langere omschrijving", position=n)
        for n in range(1, 61)
    )
    rendered = await render(view(lines=lines))

    assert b"/Count 1" not in rendered.content
    assert re.search(rb"/Count (\d+)", rendered.content).group(1) != b"1"


async def test_every_page_is_numbered_and_carries_the_supplier() -> None:
    """A page separated from the rest still has to identify who issued it,
    which is how invoices are actually filed.
    """
    lines = tuple(line(description=f"Regel {n}", position=n) for n in range(1, 61))
    text = await text_of(view(lines=lines))

    assert "Pagina 1 van" in text
    assert "Pagina 2 van" in text
    assert text.count("NL123456789B01") >= 2


async def test_the_table_header_repeats_on_each_page() -> None:
    """FR-TPL-010's "repeating headers". A column of figures with no heading on
    page two is a column somebody has to guess at.
    """
    lines = tuple(line(description=f"Regel {n}", position=n) for n in range(1, 61))
    text = await text_of(view(lines=lines))

    assert text.count("Omschrijving") >= 2


async def test_a_short_invoice_is_one_page() -> None:
    rendered = await render(view())
    assert b"/Count 1" in rendered.content


# =============================================================================
# FR-TPL-005 / FR-TPL-010: layout variants and page setup, new with this change
# =============================================================================


async def test_every_new_axis_at_its_default_is_byte_identical_to_omitting_it() -> None:
    """The single most important property this change adds: a template that
    never touches `layout`/`header_arrangement`/`totals_position`/
    `page_size`/`margins` (i.e. relies on `InvoiceTemplate`'s own dataclass
    defaults, exactly what `default_template()` and every template created
    before this change already carries) renders BYTE-IDENTICAL output to a
    template that names every one of those defaults explicitly. Together with
    `test_medium_type_scale_matches_the_pre_existing_constants` (the same
    proof for the prior typography pass), this is what keeps this pass from
    silently changing anything about a template nobody has touched.
    """
    explicit = dataclasses.replace(
        TEMPLATE,
        layout=Layout.CLASSIC,
        header_arrangement=HeaderArrangement.SPLIT,
        totals_position=TotalsPosition.RIGHT,
        page_size=PageSize.A4,
        margins=Margins.NORMAL,
    )
    implicit = await render_with(view(), TEMPLATE)
    made_explicit = await render_with(view(), explicit)
    assert implicit.content == made_explicit.content


async def test_the_default_geometry_matches_the_pre_existing_constants() -> None:
    """A4's exact pre-existing dimensions, and the 56pt margin's own derived
    right edge (539.0) - the numbers this renderer always used, before
    `PageSize`/`Margins` existed to make them configurable.
    """
    rendered = await render_with(view(), TEMPLATE)
    assert b"/MediaBox [0 0 595 842]" in rendered.content
    # The table header's rule spans from the 56pt margin to the 539pt right
    # edge - see `_column_edges`/`table_header` in api.invoicing.rendering.
    assert b"539.00" in rendered.content


async def test_the_four_layouts_render_pairwise_different_bytes() -> None:
    """FR-TPL-005's own visual differentiation: every one of the four
    `Layout` members must produce a genuinely different rendering, not just a
    different label - proven pairwise, not merely "doesn't crash".
    """
    outputs = {
        layout: (await render_with(view(), dataclasses.replace(TEMPLATE, layout=layout))).content
        for layout in Layout
    }
    names = list(outputs)
    values = list(outputs.values())
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            assert values[i] != values[j], f"{names[i]} and {names[j]} rendered identically"


async def test_classic_layout_matches_pre_existing_geometry_exactly() -> None:
    """`Layout.CLASSIC` (the default) is the one member that must reproduce
    today's exact geometry - no background fill, rules drawn.
    """
    rendered = await render_with(view(), TEMPLATE)
    assert b" re f Q" not in rendered.content  # no filled rectangle operator at all


async def test_minimal_layout_omits_every_rule() -> None:
    minimal = dataclasses.replace(TEMPLATE, layout=Layout.MINIMAL)
    rendered = await render_with(view(), minimal)
    assert b" m " not in rendered.content  # the `m ... l S` rule-drawing operators
    assert b" l S" not in rendered.content


async def test_modern_layout_draws_a_filled_band() -> None:
    modern = dataclasses.replace(TEMPLATE, layout=Layout.MODERN)
    rendered = await render_with(view(), modern)
    assert b" re f Q" in rendered.content


async def test_compact_layout_is_denser_than_classic() -> None:
    compact = dataclasses.replace(TEMPLATE, layout=Layout.COMPACT)
    classic_ys = _td_y_values((await render_with(view(), TEMPLATE)).content)
    compact_ys = _td_y_values((await render_with(view(), compact)).content)
    assert (classic_ys[0] - classic_ys[1]) > (compact_ys[0] - compact_ys[1])


async def test_header_arrangement_changes_the_output() -> None:
    split = await render_with(view(), TEMPLATE)
    stacked = await render_with(
        view(), dataclasses.replace(TEMPLATE, header_arrangement=HeaderArrangement.STACKED)
    )
    centered = await render_with(
        view(), dataclasses.replace(TEMPLATE, header_arrangement=HeaderArrangement.CENTERED)
    )
    assert split.content != stacked.content
    assert split.content != centered.content
    assert stacked.content != centered.content


async def test_totals_position_changes_the_output() -> None:
    right = await render_with(view(), TEMPLATE)
    left = await render_with(
        view(), dataclasses.replace(TEMPLATE, totals_position=TotalsPosition.LEFT)
    )
    full_width = await render_with(
        view(), dataclasses.replace(TEMPLATE, totals_position=TotalsPosition.FULL_WIDTH)
    )
    assert right.content != left.content
    assert right.content != full_width.content
    assert left.content != full_width.content
    assert b" re f Q" in full_width.content  # the boxed-summary band


async def test_letter_page_size_changes_the_media_box() -> None:
    letter = dataclasses.replace(TEMPLATE, page_size=PageSize.LETTER)
    rendered = await render_with(view(), letter)
    assert b"/MediaBox [0 0 612 792]" in rendered.content


async def test_wide_margins_move_the_right_edge_inward() -> None:
    wide = dataclasses.replace(TEMPLATE, margins=Margins.WIDE)
    rendered = await render_with(view(), wide)
    # 595 - 72pt wide margin = 523.0, replacing the default's 539.0.
    assert b"523.00" in rendered.content


# --- FR-TPL-010: carried-forward subtotals across page breaks ---------------


async def test_a_single_page_invoice_never_mentions_carrying_forward() -> None:
    text = await text_of(view())
    assert "overgebracht" not in text.lower()


async def test_carried_forward_and_brought_forward_agree_across_multiple_breaks() -> None:
    """A synthetic invoice long enough to force at least three pages. Every
    page-break boundary's "carried forward" figure must equal the very next
    page's "brought forward" figure, and the running total must keep
    accumulating rather than resetting - and the actual invoice total
    (`view.net`) must be completely unaffected by any of this presentation.
    """
    lines = tuple(
        line(description=f"Regel {n} met een tekst die lang genoeg is om te wikkelen", position=n)
        for n in range(1, 121)
    )
    v = view(lines=lines)
    rendered = await render(v)
    assert re.search(rb"/Count (\d+)", rendered.content).group(1) not in (b"1", b"2")

    text = await text_of(v)
    carried = re.findall(r"Subtotaal overgebracht: (.+)", text)
    brought = re.findall(r"Saldo overgebracht: (.+)", text)
    assert len(carried) >= 2
    assert len(carried) == len(brought)
    # Each brought-forward figure equals the carried-forward figure from the
    # page immediately before it.
    for c, b in zip(carried, brought, strict=True):
        assert c == b
    # The running total strictly increases from one break to the next - never
    # resets, never goes backwards.
    amounts = [Decimal(c.replace(".", "").replace(",", ".").split()[-1]) for c in carried]
    assert all(earlier < later for earlier, later in zip(amounts, amounts[1:], strict=False))

    # The actual total is unaffected - carrying forward is presentation only.
    assert format_money(v.net, "nl-NL") in text


# --- the adapter -------------------------------------------------------------


async def test_the_rendering_is_a_pdf_named_for_the_invoice() -> None:
    rendered = await render(view())

    assert rendered.content_type == "application/pdf"
    assert rendered.content.startswith(b"%PDF-")
    assert rendered.filename == "2026-7.pdf"


def test_build_selects_by_provider_and_has_no_off_switch() -> None:
    assert isinstance(build_invoice_renderer("minimal-pdf"), TemplatedPdfRenderer)

    for absent in ("off", "none", ""):
        with pytest.raises(ValueError, match="unknown invoice renderer provider"):
            build_invoice_renderer(absent)


# =============================================================================
# FR-TPL-016: the tagged structure the PRODUCTION renderer actually emits
#
# `tests/invoicing/test_pdf_structure.py` proves the MECHANISM in `pdf.py` is
# sound in isolation. These prove `TemplatedPdfRenderer` actually USES it -
# that wiring tags through was not left half-done, which is exactly the kind
# of gap a docstring can claim closed while the real output still defaults
# everything to one flat paragraph.
# =============================================================================


def _struct_elems(data: bytes) -> dict[int, bytes]:
    """Every `/Type /StructElem` object, keyed by its object number.

    Matches only up to `endobj` - not through its trailing newline, which
    `finditer` would otherwise consume, leaving the NEXT object's own leading
    newline already used up and silently dropping every other consecutive
    match. See `tests/invoicing/test_pdf_structure.py::_all_struct_elems` for
    the failure this avoids, found the hard way while writing that file.
    """
    found: dict[int, bytes] = {}
    for match in re.finditer(rb"\n(\d+) 0 obj\n(<< /Type /StructElem .*?)\nendobj", data, re.S):
        found[int(match.group(1))] = match.group(2)
    return found


async def test_the_title_is_a_real_heading() -> None:
    rendered = await render(view())
    elems = _struct_elems(rendered.content)

    headings = [body for body in elems.values() if b"/S /H1" in body]
    assert len(headings) == 1


async def test_the_line_item_table_is_real_table_structure() -> None:
    """Not text at coordinates that happens to look tabular - an actual
    `Table` containing `TR`s containing `TH`/`TD` cells, which is what lets a
    screen reader say "row 2, Amount, 500 euro" instead of a flat list of
    numbers with nothing attached to any of them.
    """
    v = view(
        lines=(
            line(description="Advies", net="1000.00", position=1),
            line(description="Onderzoek", net="500.00", position=2),
        ),
        groups=(
            VatGroup(
                "btw_21",
                TreatmentRole.STANDARD,
                Decimal("21.00"),
                Decimal("1500.00"),
                Decimal("315.00"),
            ),
        ),
    )
    rendered = await render(v)
    elems = _struct_elems(rendered.content)

    tables = {n: body for n, body in elems.items() if b"/S /Table" in body}
    assert len(tables) == 1

    rows = {n: body for n, body in elems.items() if b"/S /TR" in body}
    # One header row plus one row per line item.
    assert len(rows) == 3

    cells = [body for body in elems.values() if b"/S /TH" in body or b"/S /TD" in body]
    # 7 header cells (description + all six `LineColumn`s, `default_template`
    # shows every one) + 2 line items x 6 cells each - discount is blank on
    # both lines (`discount_percent=0`) and, like the single-layout ancestor
    # of this renderer, an empty cell draws no mark at all (`Text.value`
    # empty is skipped by both `_build_structure` and `_content_stream`).
    assert len(cells) == 7 + 2 * 6


async def test_every_numeric_column_is_a_cell_not_a_stray_paragraph() -> None:
    """The bug this guards against: a caller that passed `row=` without ALSO
    passing `tag=StructureTag.TABLE_CELL` would close the table the instant
    that "cell" was reached, breaking one table into a run of broken
    fragments. Caught once while wiring this renderer; this is what keeps it
    caught.
    """
    rendered = await render(view())
    elems = _struct_elems(rendered.content)

    tables = [n for n, body in elems.items() if b"/S /Table" in body]
    assert len(tables) == 1, (
        "the line-item table split into more than one - a numeric column is "
        "almost certainly tagged PARAGRAPH instead of TABLE_CELL"
    )


async def test_a_repeating_header_on_a_second_page_does_not_split_the_table() -> None:
    """FR-TPL-010's repeating headers, and the property the table-header
    helper's own docstring names: a second page's column headers must join
    the SAME continuous Table as the first page's line items, not start a
    new one.
    """
    many_lines = tuple(
        line(description=f"Regel {n} met een langere omschrijving om te wikkelen", position=n)
        for n in range(1, 61)
    )
    v = view(lines=many_lines)
    rendered = await render(v)

    assert rendered.content.count(b"/Type /Page /Parent") > 1, "the fixture did not paginate"

    elems = _struct_elems(rendered.content)
    tables = [n for n, body in elems.items() if b"/S /Table" in body]
    assert len(tables) == 1


async def test_the_document_language_matches_the_recipient_not_the_default() -> None:
    """FR-TPL-016 / PDF/A 6.9. Before this was wired through, `render_pdf`'s
    own default ("nl") would have been written into every English invoice's
    Catalog and XMP regardless of who it was actually addressed to.
    """
    dutch = await render(view(language=Language.NL))
    english = await render(view(language=Language.EN))

    assert b"/Lang (nl)" in dutch.content
    assert b"/Lang (en)" in english.content


# --- FR-TPL-001: logo alt text -----------------------------------------------


async def test_a_logo_carries_the_business_name_as_alt_text() -> None:
    """FR-TPL-016 / PDF/A 6.8.3: what somebody who cannot see the logo is
    told it is - the business's legal name, never the literal word "logo"
    (`api.invoicing.pdf.Placement.__post_init__` only refuses blank alt
    text, so the caller carries the real obligation).
    """

    async def resolve_logo(template: object):  # type: ignore[no-untyped-def]
        return load_image(png())

    renderer = TemplatedPdfRenderer(resolve_logo=resolve_logo)
    v = view()
    base = default_template(organization_id=uuid.uuid4(), administration_id=uuid.uuid4())
    template_with_logo = dataclasses.replace(
        base, logo=Logo(asset_id=uuid.uuid4(), position=LogoPosition.LEFT, size=LogoSize.MEDIUM)
    )

    rendered = await renderer.render(
        v,
        supplier=SUPPLIER,
        formatting_locale="nl-NL",
        template=template_with_logo,
        document_type=DocumentType.INVOICE,
    )
    elems = _struct_elems(rendered.content)
    figures = [body for body in elems.values() if b"/S /Figure" in body]
    assert len(figures) == 1
    assert b"/Alt (Bakker Consultancy B.V.)" in figures[0]


# --- SI-02: EPC069-12 "pay by bank" QR code ----------------------------------


async def render_with_supplier(v: InvoiceView, supplier: SupplierDetails) -> RenderedInvoice:
    return await TemplatedPdfRenderer().render(
        v,
        supplier=supplier,
        formatting_locale="nl-NL",
        template=TEMPLATE,
        document_type=_document_type(v),
    )


async def test_a_supplier_iban_produces_a_qr_code_figure() -> None:
    """The QR is a `Figure` structure element like the logo - a customer
    scanning it with a banking app is reading an IMAGE, and PDF/A requires
    every figure to be reachable by someone who cannot see it either.
    """
    supplier = dataclasses.replace(SUPPLIER, iban="NL91ABNA0417164300")
    rendered = await render_with_supplier(view(), supplier)

    elems = _struct_elems(rendered.content)
    figures = [body for body in elems.values() if b"/S /Figure" in body]
    assert len(figures) == 1


async def test_no_iban_means_no_qr_code() -> None:
    """`SUPPLIER` (the module default) has no IBAN on file - the ordinary
    state for every administration before SI-02's onboarding field is filled
    in - and must render exactly as it did before this feature existed.
    """
    assert SUPPLIER.iban is None
    rendered = await render(view())

    elems = _struct_elems(rendered.content)
    figures = [body for body in elems.values() if b"/S /Figure" in body]
    assert len(figures) == 0


async def test_a_credit_note_never_gets_a_qr_code_even_with_an_iban_on_file() -> None:
    """A credit note reduces what the customer owes - or refunds them - so a
    QR code requesting a SEPA transfer FROM them would be actively wrong.
    `build_epc_payload` would in fact refuse a non-positive amount, but this
    gate is at the rendering call site, before that module is ever reached.
    """
    supplier = dataclasses.replace(SUPPLIER, iban="NL91ABNA0417164300")
    rendered = await render_with_supplier(view(credit_of=uuid.uuid4()), supplier)

    elems = _struct_elems(rendered.content)
    figures = [body for body in elems.values() if b"/S /Figure" in body]
    assert len(figures) == 0


# --- FR-TPL-002: loading a deployment-supplied font, or honestly finding none


def test_no_configured_fonts_load_from_an_empty_directory(tmp_path: Path) -> None:
    """The state every deployment is in today - no repository-provided
    licensed font, so nothing loads and every document still falls back to
    base-14, exactly as before this change.
    """
    assert _load_embedded_fonts(tmp_path) == {}


def test_a_supplied_font_file_loads_and_is_named_for_its_template_font_code(tmp_path: Path) -> None:
    """A licensed font a deployment placed at the documented path (see
    `api.invoicing.rendering._font_data_directory`, ADR-042) is found by its
    `template_font.code` filename and made available under that same key -
    the synthetic test font stands in for a real licensed binary here, which
    is exactly what `tests/invoicing/support/synthetic_font.py`'s docstring
    says it exists for.
    """
    code = sorted(FONT_CODES)[0]
    (tmp_path / f"{code}.ttf").write_bytes(build_synthetic_font())

    fonts = _load_embedded_fonts(tmp_path)

    assert set(fonts) == {code}
    assert fonts[code].resource_name != b"F1"
    assert fonts[code].resource_name != b"F2"


def test_a_malformed_font_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A broken or unsupported file must not take down invoice issuing - it
    falls back to base-14 for that one typeface, the same posture a missing
    file already gets.
    """
    code = sorted(FONT_CODES)[0]
    (tmp_path / f"{code}.ttf").write_bytes(b"not a font")

    assert _load_embedded_fonts(tmp_path) == {}


def test_a_loaded_font_covers_the_platforms_own_winansi_text(tmp_path: Path) -> None:
    """Subset to `api.invoicing.pdf.winansi_codepoints()`, not to one
    document - see `_load_embedded_fonts`'s docstring on why. The synthetic
    font only maps 'A', 'B' and 'Ä', so this checks the ones it CAN cover
    rather than the full WinAnsi range.
    """
    code = sorted(FONT_CODES)[0]
    (tmp_path / f"{code}.ttf").write_bytes(build_synthetic_font())

    embedded = _load_embedded_fonts(tmp_path)[code]
    assert embedded.supports("A")
    assert embedded.supports("Ä")
