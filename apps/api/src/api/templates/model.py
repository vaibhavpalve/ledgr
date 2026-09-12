"""The invoice template designer's domain shape - FR-TPL-001..007, FR-TPL-009,
FR-TPL-012, FR-TPL-013, FR-TPL-005 (layout variants), FR-TPL-010 (page setup).

Mirrors migrations 0042 and 0043 (the latter adding `header_arrangement`/
`totals_position`/`page_size`/`margins` - see ADR-045). Where this module and
the database disagree, the database is right - the same bargain
`api.invoicing.model` makes with 0037.

--- Curated steps, not free entry (FR-TPL-003's philosophy, applied everywhere) ---

FR-TPL-003 asks for type controls "exposed as a small number of sensible
steps rather than free numeric entry, so a user cannot produce an unreadable
invoice." This module applies that philosophy to every dial FR-TPL-001..005
expose, not only type: logo position and size, colour is the one exception
that is checked for FORMAT rather than for VALUE (any legal `#rrggbb` is
accepted; `ColorScheme.contrast_warnings` is advisory, never a gate), and
every typography axis is a closed `enum.Enum`. A caller cannot send `size:
37.5px`; they send `TypeScale.LARGE`, and the renderer decides what points
that is. This is also why every one of these enums is mirrored by a CHECK
constraint in 0042 - the database refuses a value this module's own type
system already refuses to construct, so a write path that bypassed this
module entirely still cannot produce a step nobody designed.

--- Six line-item columns, not seven (the ADR-037 deviation) ---

FR-TPL-006 lists "quantity, unit, unit price, discount, VAT rate, VAT amount
and line total" - seven names. `LineColumn` has six members and deliberately
excludes VAT_AMOUNT. Migration 0037 (ADR-037 decision #3) does not give
`sales_invoice_line` a per-line VAT amount at all: VAT is computed and rounded
per TREATMENT GROUP, because rounding twelve lines of a few cents each and
summing them can differ by a cent from rounding the group total, and the
group total is both what EU VAT Directive Art. 226 requires an invoice to show
and what reaches the aangifte. There is therefore no per-line VAT amount to
offer a toggle for - not "hidden by default", not present in the data model at
all. Offering a column with nothing behind it would be worse than the
requirement's oversight: a template author enabling "VAT amount" would get
either a fabricated per-line split or a column of blanks. See
`docs/decisions/ADR-041-invoice-template-model.md` for the fuller argument and
`docs/decisions/ADR-037-sales-invoices.md` decision #3 for the original
reasoning this inherits unchanged.

--- FR-TPL-009 is a model concern as much as a database one ---

`STATUTORY_COLUMNS` and `REQUIRED_MERGE_TAGS` are the vocabulary
`api.templates.compliance.check()` is built from - they live here, next to the
things they describe, rather than duplicated in the compliance module, for the
reason `api.invoicing.statutory` keeps `StatutoryField` next to nothing but
itself: a set that can drift from the schema it describes is worse than no set
at all. 0042's CHECK constraints are the same vocabulary again, in SQL, as the
third and cheapest-to-verify layer of the same guarantee - see that
migration's header for the full three-layer argument.

--- Applies to the whole document family via a PARAMETER (FR-TPL-012) ---

`DocumentType` is not a column anywhere reachable from `InvoiceTemplate`. A
template is one look, and FR-TPL-012 asks for that look to be set once and
apply to invoice, credit note, quote, order confirmation, reminder and
statement alike. `DocumentType` exists so a RENDERER can be told which of the
six documents it is drawing (which heading, which merge tags resolve), not so
a template can be forked six ways to keep in step by hand.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "DocumentType",
    "Layout",
    "HeaderArrangement",
    "TotalsPosition",
    "PageSize",
    "Margins",
    "LogoPosition",
    "LogoSize",
    "TypeScale",
    "FontWeight",
    "LineHeight",
    "LetterSpacing",
    "Typography",
    "ContrastWarning",
    "ColorScheme",
    "LineColumn",
    "STATUTORY_COLUMNS",
    "ColumnSetting",
    "ColumnLayout",
    "ContentBlock",
    "REQUIRED_MERGE_TAGS",
    "BlockText",
    "Logo",
    "InvoiceTemplate",
    "TemplatesError",
    "TemplateNotFound",
    "TemplateConflict",
    "TemplateNameConflict",
    "StaleTemplateVersion",
    "FONT_CODES",
]


class DocumentType(enum.Enum):
    """FR-TPL-012's document family. A renderer argument, never a foreign key.

    Six members because that is the family the requirement names; not every
    member is built yet (quotes, order confirmations and statements are later
    PRD sections), but the type exists now so a template's shape need not
    change when they land - only what a renderer does with each does.
    """

    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    QUOTE = "quote"
    ORDER_CONFIRMATION = "order_confirmation"
    REMINDER = "reminder"
    STATEMENT = "statement"


#: FR-TPL-002's curated catalogue, mirrored from 0042's `template_font` seed
#: data. Kept here, rather than only queried from the database, so a route
#: can reject an unknown font code with a clean 422 before it ever reaches
#: 0042's foreign key - the same "refuse before the write, not instead of it"
#: posture the rest of this module takes. MUST stay in step with 0042's
#: `insert into template_font` statement; there is no runtime check that they
#: agree, because the catalogue is curated and rarely-changing by design (the
#: same trust `LineColumn` and `ContentBlock` place in their own migration's
#: CHECK constraints).
FONT_CODES: frozenset[str] = frozenset(
    {
        "inter",
        "source_sans",
        "ibm_plex_sans",
        "public_sans",
        "source_serif",
        "ibm_plex_serif",
        "pt_serif",
        "ibm_plex_mono",
        "source_code_pro",
    }
)


class Layout(enum.Enum):
    """FR-TPL-005's "at least 4 starting layouts" - a STARTING POINT, not the
    only thing FR-TPL-005 lets a user adjust. Each member also has its own
    visual character, drawn by `api.invoicing.rendering` from a small internal
    lookup (`_LAYOUT_CHARACTER`): `CLASSIC` is today's exact geometry (thin
    rules, normal spacing, no fill - the one that must byte-identically match
    pre-existing output), `MODERN` adds a tinted background band behind the
    header, `COMPACT` tightens spacing throughout without changing the
    structural arrangement, and `MINIMAL` omits every rule and fill for the
    sparest possible presentation. Independent of `HeaderArrangement` /
    `TotalsPosition` below, which a user tunes AFTER picking a starting layout.
    """

    CLASSIC = "classic"
    MODERN = "modern"
    COMPACT = "compact"
    MINIMAL = "minimal"


class HeaderArrangement(enum.Enum):
    """FR-TPL-005's header arrangement, independent of which of the 4
    `Layout` values is chosen - a layout is a starting POINT, this is one of
    the things it starts you at and that remains independently adjustable.
    """

    #: Today's only behaviour: logo + supplier address top-left, title +
    #: reference + metadata top-right. THE DEFAULT - must reproduce today's
    #: exact geometry unchanged.
    SPLIT = "split"
    #: Logo centered at the top, title below it, supplier address below that,
    #: metadata below that - one column.
    STACKED = "stacked"
    #: Logo centered, supplier address centered below it, title/reference/
    #: metadata ALSO centered, further below.
    CENTERED = "centered"


class TotalsPosition(enum.Enum):
    """FR-TPL-005's totals block position."""

    #: Today's only behaviour: subtotal/VAT/total lines right-aligned under
    #: the line-item table's amount column. THE DEFAULT.
    RIGHT = "right"
    #: The same lines, left-aligned at the page margin instead.
    LEFT = "left"
    #: A full-width band spanning margin to margin, tinted with the accent
    #: colour, with the total figures right-aligned inside it - a visually
    #: heavier "boxed summary."
    FULL_WIDTH = "full_width"


class PageSize(enum.Enum):
    """FR-TPL-010."""

    #: 595 x 842 points. THE DEFAULT - today's only size.
    A4 = "a4"
    #: 612 x 792 points (8.5in x 11in).
    LETTER = "letter"


class Margins(enum.Enum):
    """FR-TPL-010's margins, as curated steps (not a point/mm number field) -
    the same philosophy as every other control this module has.
    """

    #: 40pt.
    NARROW = "narrow"
    #: Today's existing 56pt margin. THE DEFAULT.
    NORMAL = "normal"
    #: 72pt.
    WIDE = "wide"


class LogoPosition(enum.Enum):
    """FR-TPL-001."""

    LEFT = "left"
    CENTRE = "centre"
    RIGHT = "right"


class LogoSize(enum.Enum):
    """FR-TPL-001's "size control" - steps, not a pixel or point value a user
    could pick their way into an unreadable header with.
    """

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class TypeScale(enum.Enum):
    """FR-TPL-003's size scale. Four steps: enough range to matter, few enough
    that every one of them is something a designer chose rather than
    something a slider produced.
    """

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    EXTRA_LARGE = "extra_large"


class FontWeight(enum.Enum):
    """FR-TPL-003's weight control. Three steps rather than the 100-900 axis
    a variable font exposes - a business invoice has no use for "semi-bold"
    versus "medium" as a user-facing choice.
    """

    REGULAR = "regular"
    MEDIUM = "medium"
    BOLD = "bold"


class LineHeight(enum.Enum):
    """FR-TPL-003's line height."""

    TIGHT = "tight"
    NORMAL = "normal"
    RELAXED = "relaxed"


class LetterSpacing(enum.Enum):
    """FR-TPL-003's letter spacing."""

    TIGHT = "tight"
    NORMAL = "normal"
    WIDE = "wide"


@dataclass(frozen=True, slots=True)
class Typography:
    """FR-TPL-002's "separate selection for headings, body and figures" plus
    FR-TPL-003's four controls.

    The three font fields are `template_font.code` values (0042), not
    `Typography` objects of their own - the catalogue itself is data (it can
    grow without a code change), so this module references it by the same key
    the database does rather than mirroring the row shape here.
    """

    heading_font: str
    body_font: str
    figures_font: str
    type_scale: TypeScale = TypeScale.MEDIUM
    font_weight: FontWeight = FontWeight.REGULAR
    line_height: LineHeight = LineHeight.NORMAL
    letter_spacing: LetterSpacing = LetterSpacing.NORMAL


class ContrastWarning(enum.Enum):
    """FR-TPL-004's "automatic contrast check that warns" - advisory
    identifiers, not a refusal. A caller renders these into whatever prose or
    icon its own UI wants; unlike `api.templates.compliance`'s violations,
    these never block a save; see `ColorScheme.contrast_warnings`.
    """

    TEXT_ON_BACKGROUND_LOW = "text_on_background_low"
    ACCENT_ON_BACKGROUND_LOW = "accent_on_background_low"


@dataclass(frozen=True, slots=True)
class ColorScheme:
    """FR-TPL-004. `#rrggbb`, lower-case, matching what 0042's CHECK accepts
    and what a colour picker already emits.

    `contrast_warnings` is the ONLY floating-point arithmetic in this module,
    and that is a deliberate, narrow exception to CLAUDE.md rule four (no
    financial calculation uses floating point). WCAG's relative-luminance
    formula is a display-only accessibility heuristic with no monetary
    meaning and no regulatory precision requirement - unlike a VAT amount, a
    contrast ratio that is off by a rounding hair changes nothing about what
    anybody owes, and `float` is the type every reference implementation of
    this formula uses. Using `Decimal` here would buy correctness the
    requirement does not need at the cost of implementing `pow` by hand.
    """

    accent: str
    text: str
    background: str

    @property
    def contrast_warnings(self) -> frozenset[ContrastWarning]:
        """Advisory only - FR-TPL-004 says "warns", not "refuses". Saving a
        template with poor contrast is always allowed; a caller decides what
        to do with these, typically show them inline next to the colour
        pickers that produced them.
        """
        warnings: set[ContrastWarning] = set()
        background = _relative_luminance(self.background)
        if _contrast_ratio(_relative_luminance(self.text), background) < 4.5:
            # WCAG 2.1 SC 1.4.3 (AA, normal text): 4.5:1.
            warnings.add(ContrastWarning.TEXT_ON_BACKGROUND_LOW)
        if _contrast_ratio(_relative_luminance(self.accent), background) < 3.0:
            # WCAG 2.1 SC 1.4.11 (non-text contrast) / SC 1.4.3's large-text
            # floor: 3:1. The accent colour decorates rather than carries body
            # text, so the lower bar applies.
            warnings.add(ContrastWarning.ACCENT_ON_BACKGROUND_LOW)
        return frozenset(warnings)


def _relative_luminance(hex_color: str) -> float:
    """WCAG 2.1's relative luminance for a `#rrggbb` colour, 0.0 (black) to
    1.0 (white). See https://www.w3.org/TR/WCAG21/#dfn-relative-luminance.
    """
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))

    def channel(component: float) -> float:
        return component / 12.92 if component <= 0.03928 else ((component + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast_ratio(l1: float, l2: float) -> float:
    """WCAG 2.1's contrast ratio: (L1 + 0.05) / (L2 + 0.05), lighter over
    darker, so the result is always >= 1.
    """
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


class LineColumn(enum.Enum):
    """FR-TPL-006's toggleable line-item columns - SIX, not seven.

    See this module's docstring for why VAT_AMOUNT is not a member: there is
    no per-line VAT amount in the data model for a toggle to control
    (ADR-037 decision #3).
    """

    QUANTITY = "quantity"
    UNIT = "unit"
    UNIT_PRICE = "unit_price"
    DISCOUNT = "discount"
    VAT_RATE = "vat_rate"
    LINE_TOTAL = "line_total"


#: FR-TPL-009's line-item half: the three columns Wet OB art. 35a(1) requires
#: an invoice to show per line, and therefore the three a template may not
#: hide. `unit`, `discount` and `line_total` are not independently statutory -
#: a line total is derivable from the other figures, a unit is descriptive,
#: and a discount is disclosed by the resulting price - and may be hidden
#: freely. Mirrored by 0042's `invoice_template_column_statutory_visible`
#: CHECK constraint; see this module's docstring on why the vocabulary lives
#: here rather than only in `api.templates.compliance`.
STATUTORY_COLUMNS: frozenset[LineColumn] = frozenset(
    {LineColumn.QUANTITY, LineColumn.UNIT_PRICE, LineColumn.VAT_RATE}
)


@dataclass(frozen=True, slots=True)
class ColumnSetting:
    """FR-TPL-006's show/hide/reorder, one row per column."""

    column: LineColumn
    visible: bool
    position: int


@dataclass(frozen=True, slots=True)
class ColumnLayout:
    """All six columns, exactly once each, in the order they will print.

    A value object rather than a bare tuple, so "is this a legal column
    layout at all" (every `LineColumn` present exactly once) is asked and
    answered in one place instead of by every caller that builds one.
    Whether it is a legal STATUTORY layout - none of the required three
    hidden - is a different, and blocking, question and belongs to
    `api.templates.compliance.check()`, not here: this class accepts a
    layout that hides quantity, because refusing to even CONSTRUCT one would
    make the compliance gate's job of reporting WHY impossible to reach.
    """

    settings: tuple[ColumnSetting, ...]

    def __post_init__(self) -> None:
        present = sorted(setting.column.value for setting in self.settings)
        expected = sorted(column.value for column in LineColumn)
        if present != expected:
            raise ValueError(
                f"a column layout must name every column exactly once; got {present}, "
                f"expected {expected}"
            )
        positions = sorted(setting.position for setting in self.settings)
        if positions != list(range(1, len(self.settings) + 1)):
            raise ValueError(
                f"column positions must be a gapless 1-based sequence; got {positions}"
            )

    def setting_for(self, column: LineColumn) -> ColumnSetting:
        for setting in self.settings:
            if setting.column is column:
                return setting
        raise KeyError(column)  # pragma: no cover - __post_init__ makes this unreachable

    @property
    def visible_in_order(self) -> tuple[LineColumn, ...]:
        """What a renderer actually draws, left to right."""
        return tuple(
            setting.column
            for setting in sorted(self.settings, key=lambda setting: setting.position)
            if setting.visible
        )


class ContentBlock(enum.Enum):
    """FR-TPL-007's editable content blocks."""

    HEADER = "header"
    INTRO = "intro"
    PAYMENT_TERMS = "payment_terms"
    FOOTER = "footer"
    #: FR-TPL-007's "free block for chamber of commerce number, VAT number,
    #: IBAN and general terms reference" - the one block FR-TPL-009 locks two
    #: merge tags into, because two of those four items are statutory
    #: (art. 35a(1)(c), (e) and the Handelsregisterwet) and the rest of the
    #: block is free prose around them.
    LEGAL_IDENTITY = "legal_identity"


#: FR-TPL-007's merge tags, and FR-TPL-009 applied to free text: the tags a
#: block's text MUST still contain, in both languages, for the template to be
#: compliant. Empty for every block except `legal_identity` - the other four
#: are free prose with no mandatory content, however useful their own merge
#: tags (customer name, invoice number, due date, amounts) are.
#:
#: Mirrored by 0042's `invoice_template_block_legal_identity_tags` CHECK
#: constraint, which matches on the literal `{{tag}}` string because the tag
#: syntax is fixed even though the surrounding prose is not.
REQUIRED_MERGE_TAGS: dict[ContentBlock, frozenset[str]] = {
    ContentBlock.HEADER: frozenset(),
    ContentBlock.INTRO: frozenset(),
    ContentBlock.PAYMENT_TERMS: frozenset(),
    ContentBlock.FOOTER: frozenset(),
    ContentBlock.LEGAL_IDENTITY: frozenset({"supplier_vat_number", "supplier_kvk_number"}),
}


@dataclass(frozen=True, slots=True)
class BlockText:
    """FR-TPL-007 and FR-TPL-013: one block, both languages on the one row.

    Both languages sit together for the same reason `sales_invoice`'s
    `customer_language` note gives at the document level: rendering follows
    the RECIPIENT's language, so a template with only one language of a
    mandatory block would make every document to a reader of the other
    language non-compliant by construction.
    """

    block: ContentBlock
    text_nl: str
    text_en: str

    def text_for(self, language_code: str) -> str:
        """`language_code` is `"nl"` or `"en"` - a bare string rather than
        `api.i18n.language.Language`, so this module (and 0042's schema it
        mirrors) does not need to depend on the i18n package for a two-way
        switch. `api.templates.compliance` is where `Language` and this meet.
        """
        return self.text_nl if language_code == "nl" else self.text_en


@dataclass(frozen=True, slots=True)
class Logo:
    """FR-TPL-001. `asset_id` is None for a template with no logo yet - the
    designer's starting state, not an error."""

    asset_id: uuid.UUID | None
    position: LogoPosition = LogoPosition.LEFT
    size: LogoSize = LogoSize.MEDIUM


@dataclass(frozen=True, slots=True)
class InvoiceTemplate:
    """The whole designed object - FR-TPL-001..007, one administration's one
    named template.

    `columns` and `blocks` are required to be complete (all six columns, all
    five blocks) by their own types (`ColumnLayout.__post_init__`) and by the
    repository that assembles this from 0042's rows - never partial, because
    a template missing a block is not "unfinished", it is a template that
    cannot be checked for FR-TPL-009 compliance at all.
    """

    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    name: str
    is_default: bool
    #: Bumped by 0042's trigger on every UPDATE. Optimistic concurrency, not a
    #: re-render signal - see 0042's header comment.
    version: int
    layout: Layout
    logo: Logo
    typography: Typography
    colors: ColorScheme
    columns: ColumnLayout
    blocks: tuple[BlockText, ...]
    #: FR-TPL-005's header arrangement - independently adjustable from
    #: `layout` above. Defaults to `SPLIT`, today's only geometry.
    header_arrangement: HeaderArrangement = HeaderArrangement.SPLIT
    #: FR-TPL-005's totals block position. Defaults to `RIGHT`, today's only
    #: placement.
    totals_position: TotalsPosition = TotalsPosition.RIGHT
    #: FR-TPL-010's page size. Defaults to `A4`, today's only size.
    page_size: PageSize = PageSize.A4
    #: FR-TPL-010's margins. Defaults to `NORMAL`, today's existing 56pt.
    margins: Margins = Margins.NORMAL
    updated_by_user_id: uuid.UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def block_text(self, block: ContentBlock) -> BlockText | None:
        for candidate in self.blocks:
            if candidate.block is block:
                return candidate
        return None


class TemplatesError(Exception):
    """Base for this package's refusals."""


class TemplateNotFound(TemplatesError):
    """No such template in this administration.

    Indistinguishable from "belongs to another tenant", deliberately: RLS
    filters it out either way, so this branch cannot tell and must not seem
    to - the same argument `api.invoicing.model.InvoiceNotFound` makes.
    """


class TemplateConflict(TemplatesError):
    """FR-TPL-011: at most one default template per administration.

    Raised when a write would produce a second one - 0042's partial unique
    index is what actually prevents the row, this is the domain-shaped
    refusal a service raises after catching that constraint.
    """


class TemplateNameConflict(TemplatesError):
    """Two templates in one administration cannot share a name - 0043's
    `invoice_template_name_unique` is what actually prevents the row.

    FR-TPL-019's `duplicate` catches this itself and retries with a
    disambiguated name ("Copy of X (2)", "(3)", ...) before ever surfacing it
    to a caller; `create_template`/`update_template` have no such retry (a
    user picked the colliding name deliberately) and translate it into its
    own 409, distinct from `TemplateConflict`'s default-slot message.
    """


class StaleTemplateVersion(TemplatesError):
    """Optimistic concurrency: somebody else saved this template first.

    `version` (0042, bumped by trigger on every UPDATE) is what this checks
    against - see 0042's header comment on why that column exists and what it
    deliberately does NOT control (an issued invoice's appearance, which
    FR-TPL-017 freezes independently of any template edit).
    """

    def __init__(self, template_id: uuid.UUID, expected_version: int) -> None:
        self.template_id = template_id
        self.expected_version = expected_version
        super().__init__(
            f"template {template_id} was saved by somebody else after version "
            f"{expected_version} was read. Reload it and reapply your changes."
        )
