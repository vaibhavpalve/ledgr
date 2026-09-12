"""A minimal PDF writer - the mechanics behind FR-TPL-017's stored rendering.

This module knows nothing about invoices. It draws text on A4 pages and
serialises them, so that `api.invoicing.rendering` can be about what an invoice
LOOKS like without also being about cross-reference tables.

--- Why hand-written rather than a library ---

The document this produces is deliberately plain, and every PDF library that
could produce it brings a rendering engine, font subsetting and an image
pipeline for a page of text in one font. The whole format used here is a
handful of operators (`BT`/`Tf`/`Td`/`Tj`/`ET`), the base-14 fonts that every
conforming reader already has, and an xref table. That is a small enough
surface to read in one sitting, which matters more than usual for bytes that a
customer receives and that CMP-001 keeps for seven years.

--- FR-TPL-016: tagged, unconditionally ---

Every page is written with a structure tree, a `/StructParents` link back to
it, and a ToUnicode CMap - so a reader's reading order matches what is on the
page rather than raw content order, text copies out as what it says rather
than as WinAnsi bytes, and a screen reader has something to read at all. This
is true of every document this writer produces, archival or not - see
`StructureTag`, `_build_structure` and `render_pdf`'s docstring.

--- FR-TPL-015: PDF/A is a claim this writer will not make falsely ---

`render_pdf` takes a `Conformance` and refuses to emit one it cannot support -
see `api.invoicing.pdfa`, which owns that gate and the reasoning behind it.
Today that gate always refuses an archival request: PDF/A requires an embedded
font, and this writer relies on the base-14 fonts every reader already has.
That is a licensing gap (a font this repository does not have the right to
embed), not a coding one, and closing it is the only change either module will
need before an archival claim can succeed.

--- Deterministic output ---

Nothing here reads the clock or a random source, so the same page list
serialises to the same bytes every time. That is what lets
`tests/invoicing/test_pdf.py` assert on structure at all, and it is a small
independent check on FR-TPL-017: a renderer whose output drifted between two
runs over identical input could not be storing "the PDF as issued" in any
meaningful sense.

--- WinAnsi, and what it cannot spell ---

The base-14 fonts are used with `WinAnsiEncoding`, which covers Latin-1 plus
the Windows-1252 additions - so the euro sign, the Dutch ij digraph as two
letters, and every accented character in a Dutch or German company name are all
representable. A character outside it is transliterated rather than dropped
(see `_encode`): a name rendered slightly wrong is a problem somebody can see
and report, and a name silently missing a character is one they cannot.
"""

from __future__ import annotations

import enum
import hashlib
import struct
import unicodedata
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from api.invoicing.pdfa import (
    Conformance,
    assert_conformance,
    output_intent,
    to_unicode_cmap,
    to_unicode_cmap_cid,
    xmp_packet,
)
from api.invoicing.truetype import EmbeddedFont

__all__ = [
    "A4_WIDTH",
    "A4_HEIGHT",
    "LETTER_WIDTH",
    "LETTER_HEIGHT",
    "Font",
    "Color",
    "StructureTag",
    "Text",
    "Line",
    "Image",
    "ImageError",
    "Placement",
    "Page",
    "Conformance",
    "EmbeddedFont",
    "load_image",
    "render_pdf",
    "winansi_codepoints",
]

#: A4 in PostScript points (1/72 inch), the unit every PDF coordinate is in.
#: FR-TPL-010 also names US Letter (`LETTER_WIDTH`/`LETTER_HEIGHT` below) -
#: `Page.page_width`/`Page.page_height` (defaulting to these A4 values) is
#: what makes page size a per-page PARAMETER rather than a module constant, so
#: every existing caller that never asks for Letter is completely unaffected.
A4_WIDTH = 595
A4_HEIGHT = 842

#: US Letter, 8.5in x 11in at 72 points/inch - FR-TPL-010's second page size.
LETTER_WIDTH = 612
LETTER_HEIGHT = 792


class Font:
    """The two base-14 fonts this writer uses.

    Base-14 means every conforming reader already has them, so nothing is
    embedded and the file stays small. It also means no font licence question
    and no subsetting bug - the two most common ways a generated PDF fails to
    open on somebody else's machine.
    """

    REGULAR = "F1"
    BOLD = "F2"


@dataclass(frozen=True, slots=True)
class Color:
    """A fill/stroke colour - `rg`/`RG`'s own operand shape (0.0-1.0 per
    channel), so a caller never hands this module anything it has to convert.

    `api.templates.model.ColorScheme` carries `#rrggbb` strings (a colour
    picker's own vocabulary); `from_hex` is the one place that format meets
    this one. `None` is the default everywhere a `Color` is optional and means
    "leave the current graphics state alone" - which for text and rules is
    black, PDF's own initial fill/stroke colour, so a caller that never
    supplies one draws in exactly the black this writer always has.
    """

    red: float
    green: float
    blue: float

    @staticmethod
    def from_hex(value: str) -> Color:
        """`#rrggbb`, the only shape `api.templates.model.ColorScheme`
        accepts (its own CHECK-constraint-mirrored validation already refuses
        anything else before this is ever called).
        """
        text = value.lstrip("#")
        if len(text) != 6:
            raise ValueError(f"{value!r} is not a #rrggbb colour")
        red, green, blue = (int(text[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
        return Color(red, green, blue)

    def _operand(self) -> bytes:
        return b"%.4f %.4f %.4f" % (self.red, self.green, self.blue)


class StructureTag(enum.Enum):
    """What a piece of content IS, for FR-TPL-016's tagged structure.

    A screen reader has nothing to go on but this. Untagged, an invoice is a
    scattering of text at coordinates, and the reading order it announces is
    whatever order the content stream happens to be in - which for a
    two-column header is the supplier's address interleaved with the customer's.

    The set is deliberately small: PDF's standard structure types run to
    dozens, and every one this writer does not use is one nobody has to decide
    about. `RoleMap` is emitted anyway (PDF/A 6.8 asks for it) so a reader that
    does not know a type can still resolve it to a standard one.
    """

    DOCUMENT = "Document"
    HEADING = "H1"
    SUBHEADING = "H2"
    PARAGRAPH = "P"
    TABLE = "Table"
    TABLE_ROW = "TR"
    TABLE_HEADER = "TH"
    TABLE_CELL = "TD"
    FIGURE = "Figure"

    @property
    def is_cell(self) -> bool:
        return self in (StructureTag.TABLE_HEADER, StructureTag.TABLE_CELL)


@dataclass(frozen=True, slots=True)
class Text:
    """One run of text at one position.

    `x` and `y` are PDF coordinates: the origin is the BOTTOM-left of the page
    and y grows upward, which is the opposite of every screen coordinate system
    and the single most common source of upside-down output. The renderer above
    works in "distance from the top" and converts once, in `Page.at`.
    """

    x: float
    y: float
    value: str
    #: A base-14 resource name (`Font.REGULAR`/`Font.BOLD`) or an embedded
    #: font's own `EmbeddedFont.resource_name` (as `str`, e.g. `"F3"`) - see
    #: `render_pdf`'s `embedded_fonts` parameter. `render_pdf` decides HOW to
    #: encode `value` (WinAnsi bytes or Identity-H CIDs) from which of the two
    #: this is; this dataclass itself does not need to know.
    font: str = Font.REGULAR
    #: `None` draws in the current default (black) - see `Color`.
    color: Color | None = None
    #: FR-TPL-016 sets a minimum effective body size of 9pt. Enforced in
    #: `__post_init__` rather than trusted, because the way a layout breaks that
    #: rule is by shrinking a column to make it fit.
    size: float = 10.0
    #: What this run is, for the structure tree. Defaults to a paragraph
    #: because that is what most of an invoice is, and because a wrong-but-
    #: plausible tag is better than none: a reader given no tag at all falls
    #: back to raw content order.
    tag: StructureTag = StructureTag.PARAGRAPH
    #: Which table row this cell belongs to. Cells sharing a row number become
    #: one `TR`, which is what lets a screen reader say "row 3, Amount, 121
    #: euro" instead of reading a column of numbers with nothing attached.
    #: None for anything that is not a cell.
    row: int | None = None
    #: FR-TPL-003's letter spacing, as a PDF `Tc` (character spacing) value in
    #: points. `0.0` - the default, and what every caller before this field
    #: existed implicitly meant - emits NO `Tc` operator at all
    #: (`_content_stream` only writes one when this is non-zero), so a run
    #: that does not ask for letter spacing draws byte-identically to before
    #: this field existed. Additive, exactly the precedent `Color` set: see
    #: `tests/invoicing/test_pdf.py::
    #: test_no_letter_spacing_text_is_byte_identical_to_before_letter_spacing_existed`.
    #: Usually set via `Page.at`'s `letter_spacing` parameter, or inherited
    #: from `Page.default_letter_spacing` when a caller does not name one of
    #: its own - never trusted to be within any particular range here, because
    #: `api.invoicing.rendering` maps FR-TPL-003's three curated
    #: `LetterSpacing` steps to values chosen to stay readable, and this
    #: dataclass has no basis of its own for judging a raw float.
    letter_spacing: float = 0.0
    #: Running headers/footers and page numbers - repeated on every page, not
    #: part of the document's reading flow, and not something a screen reader
    #: should announce between every section any more than it should announce
    #: a rule (`Line`, which has no tag at all for exactly this reason).
    #:
    #: `tag`/`row` are ignored when this is set: `_content_stream` wraps the
    #: mark as `/Artifact` instead of tagged content, and `_build_structure`
    #: skips it entirely - no MCID, no structure node. That second part is
    #: not only correct accessibility practice, it is load-bearing for
    #: FR-TPL-010's repeating table headers: a footer drawn between the last
    #: line item on one page and the header repeated on the next must NOT
    #: read as ordinary paragraph content, or `_build_structure`'s rule that
    #: any non-cell content closes an open table would end the line-item
    #: Table at every page break, splitting one table into as many pieces as
    #: there are pages.
    is_artifact: bool = False

    def __post_init__(self) -> None:
        if self.size < 9.0:
            raise ValueError(
                f"{self.size}pt is below FR-TPL-016's 9pt minimum effective body "
                f"size. Shrinking text to make a column fit is the failure that "
                f"requirement names; re-flow the column instead."
            )
        if self.is_artifact:
            return
        if self.tag.is_cell and self.row is None:
            raise ValueError(
                f"a {self.tag.value} needs a row number: cells are grouped into "
                f"table rows by it, and an ungrouped cell is read out with no "
                f"row context at all (FR-TPL-016)."
            )


@dataclass(frozen=True, slots=True)
class Line:
    """A horizontal rule."""

    x1: float
    y: float
    x2: float
    width: float = 0.5
    #: `None` draws in the current default (black) - see `Color`.
    color: Color | None = None


@dataclass(frozen=True, slots=True)
class Rect:
    """A filled rectangle - the one shape this writer draws besides text,
    rules and images, for `template.colors.background`'s "paper colour" when
    it is something other than white.

    Like a rule, a filled rectangle is an ARTIFACT (`_content_stream` marks it
    as such): it carries no meaning a screen reader should announce, and it
    must not close an open line-item Table for the same reason a rule and a
    running footer do not - see `_build_structure`'s docstring.
    """

    x: float
    y: float
    width: float
    height: float
    color: Color


class ImageError(ValueError):
    """Bytes this writer will not embed.

    Every raise names the fix, because the person who meets one is uploading a
    logo (FR-TPL-001) and "unsupported image" is the dead end D5 rules out.
    Refusing is the right posture rather than embedding something a reader
    might render: a logo that comes out as a black rectangle, or as a
    photographic negative, is worse on a customer's invoice than one that was
    rejected at upload with a sentence saying what to export instead.
    """


@dataclass(frozen=True, slots=True)
class Image:
    """A raster ready to become a PDF image XObject.

    Built by `load_image` and never by hand: the fields below are PDF's
    vocabulary, not an image format's, and getting `decode_parms` wrong
    produces a picture that is skewed rather than absent.

    `data` is ALREADY in the encoding `filter_name` names - the JPEG's own
    compressed bytes, or a PNG's own zlib stream. Neither is re-encoded, which
    is what keeps this writer free of an image pipeline and keeps a logo
    byte-identical between the file somebody uploaded and the invoice it lands
    on.
    """

    width: int
    height: int
    #: The stream payload, already compressed.
    data: bytes
    #: `DCTDecode` for JPEG, `FlateDecode` for PNG.
    filter_name: bytes
    #: `/DeviceRGB`, `/DeviceGray`, `/DeviceCMYK`, or an `[/Indexed ...]` array.
    colour_space: bytes
    bits: int
    #: PNG's predictor, which PDF understands natively - see `_png`.
    decode_parms: bytes | None = None
    #: The alpha channel, as its own greyscale image. PDF cannot take
    #: interleaved alpha, so RGBA is split into two streams.
    smask: Image | None = None
    #: `[1 0 1 0 1 0 1 0]` for an Adobe CMYK JPEG, which stores inverted.
    decode: bytes | None = None
    #: `/Mask` index ranges, for an indexed PNG whose transparency is binary.
    mask: bytes | None = None

    @property
    def digest(self) -> bytes:
        """Identity, for sharing one XObject across pages.

        A logo repeated on every page of a six-page invoice is one object and
        one copy of the bytes. Hashed rather than compared by identity so two
        equal images loaded separately still share.
        """
        return hashlib.sha256(
            self.data + self.colour_space + str((self.width, self.height, self.bits)).encode()
        ).digest()

    def fit(self, *, max_width: float, max_height: float) -> tuple[float, float]:
        """Display size in points that fits the box without distorting.

        FR-TPL-001's "size control" gives a box; this turns it into a size. A
        logo stretched to fill a box is the single most visible way a generated
        document looks wrong, so aspect ratio is preserved here rather than
        left to each caller to remember.
        """
        if self.width <= 0 or self.height <= 0:  # pragma: no cover - load_image refuses
            raise ImageError("an image with no extent cannot be placed")
        scale = min(max_width / self.width, max_height / self.height)
        return (self.width * scale, self.height * scale)


@dataclass(frozen=True, slots=True)
class Placement:
    """One image, at one size, on one page.

    `width` and `height` are DISPLAY size in points and have nothing to do with
    the pixel dimensions: PDF draws an image into a unit square scaled by the
    current transformation matrix, so the same `Image` can appear at two sizes
    without being re-encoded.
    """

    image: Image
    x: float
    y: float
    width: float
    height: float
    #: FR-TPL-016 / PDF/A 6.8.3: what somebody who cannot see this is told it
    #: is. Required rather than optional - a figure with no alternative text is
    #: an untagged hole in an otherwise tagged document, and defaulting it to
    #: "image" would satisfy the validator while telling a screen-reader user
    #: nothing.
    alt: str = ""

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ImageError(
                f"an image placed at {self.width}x{self.height}pt would be invisible. "
                f"A logo that silently vanishes is worse than one that is refused."
            )
        if not self.alt.strip():
            raise ImageError(
                "an image needs alternative text describing it (FR-TPL-016). For a "
                "logo the business name is the right answer, not 'logo'."
            )


@dataclass
class Page:
    texts: list[Text] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    images: list[Placement] = field(default_factory=list)
    rects: list[Rect] = field(default_factory=list)
    #: FR-TPL-003's letter spacing, applied to every `at()` call on this page
    #: that does not name its own `letter_spacing` - the mechanism that lets
    #: `api.invoicing.rendering` set one template-wide value once, at each
    #: `Page()` it constructs, instead of repeating it at every one of a
    #: layout's many `page.at(...)` call sites. `0.0` (the default) means
    #: every `Text` this page produces keeps its own `0.0` default too, so a
    #: page nobody configures draws byte-identically to before this field
    #: existed.
    default_letter_spacing: float = 0.0
    #: FR-TPL-010's page size. `A4_WIDTH`/`A4_HEIGHT` are the defaults - every
    #: caller that never asks for `LETTER_WIDTH`/`LETTER_HEIGHT` draws exactly
    #: as it did before this pair of fields existed. Only `page_height` is
    #: actually used by this class's own methods (the top-to-bottom flip);
    #: `page_width` travels with the page purely so `render_pdf` can size this
    #: page's own `/MediaBox` from the `Page` itself rather than a second,
    #: separately-threaded parameter.
    page_width: int = A4_WIDTH
    page_height: int = A4_HEIGHT

    def at(
        self,
        *,
        x: float,
        top: float,
        value: str,
        font: str = Font.REGULAR,
        size: float = 10.0,
        color: Color | None = None,
        tag: StructureTag = StructureTag.PARAGRAPH,
        row: int | None = None,
        is_artifact: bool = False,
        letter_spacing: float | None = None,
    ) -> None:
        """Place text `top` points below the top edge.

        The whole page is laid out downward from the top, which is how a person
        describes a document, and the flip to PDF's bottom-left origin happens
        here and nowhere else.

        `is_artifact=True` is for running headers/footers and page numbers -
        see `Text.is_artifact`. Everything else on an invoice is real content
        and takes the default.

        `letter_spacing=None` (the default) means "use this page's own
        `default_letter_spacing`" - not "zero regardless of the page". An
        explicit value, when a caller has one, always wins.
        """
        self.texts.append(
            Text(
                x=x,
                y=self.page_height - top,
                value=value,
                font=font,
                color=color,
                size=size,
                tag=tag,
                row=row,
                is_artifact=is_artifact,
                letter_spacing=(
                    self.default_letter_spacing if letter_spacing is None else letter_spacing
                ),
            )
        )

    def rule(
        self, *, x1: float, top: float, x2: float, width: float = 0.5, color: Color | None = None
    ) -> None:
        """A horizontal rule.

        Rules are ARTIFACTS, not content: they carry no meaning a reader should
        announce, and tagging one would make a screen reader say "line" between
        every section. `_content_stream` marks them as such, which is what keeps
        them out of the structure tree while still satisfying PDF/A's rule that
        every mark is either tagged or explicitly an artifact.
        """
        self.lines.append(Line(x1=x1, y=self.page_height - top, x2=x2, width=width, color=color))

    def fill_rect(self, *, x: float, top: float, width: float, height: float, color: Color) -> None:
        """A filled rectangle, TOP-left corner `top` points below the top edge
        - `place_image`'s convention, for the same reason: callers describe a
        page top-down throughout, and the flip happens once, here.
        """
        self.rects.append(
            Rect(x=x, y=self.page_height - top - height, width=width, height=height, color=color)
        )

    def place_image(
        self,
        *,
        image: Image,
        x: float,
        top: float,
        width: float,
        height: float,
        alt: str,
    ) -> None:
        """Place an image with its TOP-left corner `top` below the top edge.

        Top-left rather than PDF's bottom-left, so callers describe a page one
        way throughout - and the flip accounts for the image's height, which is
        the part that is easy to get wrong when each caller does it.

        `alt` has no default: see `Placement`.
        """
        self.images.append(
            Placement(
                image=image,
                x=x,
                y=self.page_height - top - height,
                width=width,
                height=height,
                alt=alt,
            )
        )


#: Widths of the Helvetica glyphs, in 1/1000 em, for the ASCII range. Enough to
#: right-align a column of figures and to know when a description needs
#: wrapping, which are the only two measurements this writer takes.
#:
#: A partial table on purpose: the alternative is embedding all of Helvetica's
#: metrics for a document whose variable text is company names and product
#: descriptions. Anything absent falls back to `_DEFAULT_WIDTH`, which is the
#: width of a digit - so a measurement is never wildly wrong, only slightly.
_DEFAULT_WIDTH = 556
_WIDTHS: dict[str, int] = {
    " ": 278,
    "!": 278,
    '"': 355,
    "#": 556,
    "$": 556,
    "%": 889,
    "&": 667,
    "'": 191,
    "(": 333,
    ")": 333,
    "*": 389,
    "+": 584,
    ",": 278,
    "-": 333,
    ".": 278,
    "/": 278,
    ":": 278,
    ";": 278,
    "<": 584,
    "=": 584,
    ">": 584,
    "?": 556,
    "@": 1015,
    "[": 278,
    "\\": 278,
    "]": 278,
    "^": 469,
    "_": 556,
    "`": 333,
    "{": 334,
    "|": 260,
    "}": 334,
    "~": 584,
    "a": 556,
    "b": 556,
    "c": 500,
    "d": 556,
    "e": 556,
    "f": 278,
    "g": 556,
    "h": 556,
    "i": 222,
    "j": 222,
    "k": 500,
    "l": 222,
    "m": 833,
    "n": 556,
    "o": 556,
    "p": 556,
    "q": 556,
    "r": 333,
    "s": 500,
    "t": 278,
    "u": 556,
    "v": 500,
    "w": 722,
    "x": 500,
    "y": 500,
    "z": 500,
    "A": 667,
    "B": 667,
    "C": 722,
    "D": 722,
    "E": 667,
    "F": 611,
    "G": 778,
    "H": 722,
    "I": 278,
    "J": 500,
    "K": 667,
    "L": 556,
    "M": 833,
    "N": 722,
    "O": 778,
    "P": 667,
    "Q": 778,
    "R": 722,
    "S": 667,
    "T": 611,
    "U": 722,
    "V": 667,
    "W": 944,
    "X": 667,
    "Y": 667,
    "Z": 611,
    "€": 556,
}


def text_width(value: str, size: float) -> float:
    """Approximate rendered width in points.

    Approximate is stated rather than hidden: it drives right-alignment and
    wrapping, where being a point or two out shifts a column slightly, and it
    drives nothing that has to be exact. Bold is wider than regular and this
    does not model that, so a bold heading measured with these widths lands
    marginally left of where it would if it were exact.
    """
    return sum(_WIDTHS.get(char, _DEFAULT_WIDTH) for char in value) * size / 1000.0


def wrap(value: str, *, width: float, size: float) -> list[str]:
    """Break `value` into lines that fit `width` points.

    Breaks on spaces, and hard-breaks a single word longer than the column -
    a product code with no spaces in it must not run off the page and out of
    the printable area, where it would be silently absent from the document
    rather than merely ugly.
    """
    if not value:
        return [""]

    lines: list[str] = []
    current = ""
    for word in value.split():
        candidate = f"{current} {word}".strip()
        if current and text_width(candidate, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate

        while text_width(current, size) > width and len(current) > 1:
            cut = len(current)
            while cut > 1 and text_width(current[:cut], size) > width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]

    if current:
        lines.append(current)
    return lines or [""]


#: Windows-1252's additions in the 0x80-0x9F range, which Latin-1 leaves as
#: control characters. The euro is the one that matters on an invoice.
_WINANSI_EXTRA: dict[str, int] = {
    "€": 0x80,
    "‚": 0x82,
    "ƒ": 0x83,
    "„": 0x84,
    "…": 0x85,
    "†": 0x86,
    "‡": 0x87,
    "ˆ": 0x88,
    "‰": 0x89,
    "Š": 0x8A,
    "‹": 0x8B,
    "Œ": 0x8C,
    "Ž": 0x8E,
    "‘": 0x91,
    "’": 0x92,
    "“": 0x93,
    "”": 0x94,
    "•": 0x95,
    "–": 0x96,
    "—": 0x97,
    "˜": 0x98,
    "™": 0x99,
    "š": 0x9A,
    "›": 0x9B,
    "œ": 0x9C,
    "ž": 0x9E,
    "Ÿ": 0x9F,
}


def _winansi_to_unicode() -> dict[int, int]:
    """Every WinAnsi byte and the character it means.

    Built from the same three ranges `_encode` writes into - ASCII, Latin-1,
    and the Windows-1252 additions above - so there is one definition of the
    encoding rather than two that could drift. Fed to
    `api.invoicing.pdfa.to_unicode_cmap`, which owns the CMap FORMAT and knows
    nothing about WinAnsi; this function is the one place that knows both.
    """
    mapping = {code: code for code in range(32, 127)}
    mapping.update({code: code for code in range(160, 256)})
    mapping.update({byte: ord(char) for char, byte in _WINANSI_EXTRA.items()})
    return mapping


def winansi_codepoints() -> frozenset[int]:
    """Every Unicode code point this writer's base-14/WinAnsi path can
    already spell - the SAME set `_winansi_to_unicode` builds, as code points
    rather than byte codes.

    Public because `api.invoicing.rendering`'s font loader uses it as the
    subset target for a deployment-supplied `template_font` binary: a
    renderer built once (`build_invoice_renderer`) and reused for every
    invoice issued in the process's lifetime cannot re-subset per document
    without re-parsing the font on every request, so it commits upfront to
    covering everything the platform's OWN text pipeline can already
    produce - genuinely a subset (a few hundred glyphs), never the font's
    whole multi-script coverage.
    """
    return frozenset(_winansi_to_unicode().values())


#: Letters that NFKD does NOT decompose, because the mark is part of the letter
#: rather than a combining accent - a stroke, a bar, a ligature. Without these
#: `ł` normalises to itself, encodes to nothing, and becomes `?`, so a Polish
#: customer's name loses a letter.
#:
#: Short on purpose: these are the ones reachable from European company names
#: that Latin-1 does not already cover. Latin-1 handles ø, æ, ß, þ and every
#: accented vowel, so none of those belong here.
_TRANSLITERATIONS: dict[str, str] = {
    "Ł": "L",
    "ł": "l",
    "Đ": "D",
    "đ": "d",
    "Ħ": "H",
    "ħ": "h",
    "Ŧ": "T",
    "ŧ": "t",
    "Œ": "OE",
    "œ": "oe",
    "İ": "I",
    "ı": "i",
    "Ŋ": "NG",
    "ŋ": "ng",
    "‐": "-",
    "‑": "-",
    "−": "-",
}


def _encode(value: str) -> bytes:
    """A string as WinAnsi bytes, transliterating what it cannot spell.

    Four passes, each a fallback for the one before: the Windows-1252
    additions, then Latin-1, then the explicit table above, then an NFKD
    decomposition that strips combining accents. A character surviving none of
    them becomes `?`, which is visible.

    Dropping the character silently is what this exists to avoid. A customer
    name rendered `Kowalski` when it should read `Kowałski` is wrong in a way
    somebody notices and reports; one rendered `Kowaski` is wrong in a way that
    looks like a typo nobody made.
    """
    out = bytearray()
    for char in value:
        extra = _WINANSI_EXTRA.get(char)
        if extra is not None:
            out.append(extra)
            continue
        try:
            out.extend(char.encode("latin-1"))
            continue
        except UnicodeEncodeError:
            pass

        replacement = _TRANSLITERATIONS.get(char)
        if replacement is not None:
            out.extend(replacement.encode("latin-1"))
            continue

        folded = unicodedata.normalize("NFKD", char)
        ascii_form = folded.encode("ascii", "ignore")
        out.extend(ascii_form or b"?")
    return bytes(out)


def _escape(value: str) -> bytes:
    r"""PDF string-literal escaping: `\`, `(` and `)`.

    Unbalanced parentheses in a company name would otherwise terminate the
    string early and corrupt every operator after it - the PDF equivalent of an
    unescaped quote, and just as easy to hit with real data ("Bakker (Holding)
    B.V." is an ordinary name).
    """
    encoded = _encode(value)
    return encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


# =============================================================================
# Images - FR-TPL-001's logo
# =============================================================================
#
# --- Neither format is re-encoded, and that is the whole trick -------------
#
# PDF's image filters are the same two compressors these formats already use:
#
#     JPEG   `DCTDecode` IS JPEG. The file's own entropy-coded bytes are the
#            stream, unmodified.
#     PNG    `FlateDecode` is zlib, and - crucially - PDF implements PNG's own
#            PREDICTORS. So a PNG's IDAT data, filtered scanlines and all, is a
#            valid PDF stream given `/Predictor 15`.
#
# So this writer embeds pictures without decoding one, which is what keeps it
# free of an image pipeline. A logo's bytes on the invoice are the bytes that
# were uploaded.
#
# The one case where that fails is ALPHA. PDF has no interleaved alpha channel:
# transparency is a second, greyscale image in `/SMask`. An RGBA PNG therefore
# has to be decompressed, un-filtered, and split - the only path here that
# looks at a pixel. FR-TPL-001 asks for "a transparent-background preview",
# so it is not an edge case; it is what a logo usually is.
#
# --- What is refused, and why refusing is right ----------------------------
#
# Interlaced PNG, progressive JPEG, and per-index partial alpha are all
# refused by name, with the export setting that fixes them. Each could be
# supported by decoding further; none is worth it for a logo, and a picture
# that renders in one reader and not another is the worst outcome on a document
# a customer has to be able to open.
#
# SVG is not here at all. It is not a raster and cannot be embedded: it needs
# rasterising or converting to path operators, which is a different piece of
# work - see ADR-041's gaps.


def load_image(data: bytes) -> Image:
    """A PNG or JPEG, ready to place.

    The format is decided from the BYTES, never from a filename or a declared
    type - the same posture `api.documents.content_type` takes and for the same
    reason: the extension is a claim by whoever uploaded the file.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png(data)
    if data.startswith(b"\xff\xd8\xff"):
        return _jpeg(data)
    raise ImageError(
        "this file is neither a PNG nor a JPEG. Export the logo as PNG (with "
        "transparency if you need it) or JPEG."
    )


# --- JPEG --------------------------------------------------------------------

#: Start-of-frame markers that carry dimensions AND that DCTDecode reads.
#: SOF0/1 are baseline and extended sequential. SOF2 (progressive) is
#: deliberately absent - see `_jpeg`.
_JPEG_SOF = frozenset({0xC0, 0xC1})
#: Markers that stand alone: no length, no payload.
_JPEG_STANDALONE = frozenset({0xD8, 0xD9} | set(range(0xD0, 0xD8)))
_JPEG_COLOUR_SPACES = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}


def _jpeg(data: bytes) -> Image:
    """A baseline JPEG, embedded whole.

    The scan is only for the frame header: DCTDecode consumes the file itself,
    so nothing here touches the compressed data.
    """
    position = 2
    adobe = False

    while position < len(data) - 1:
        if data[position] != 0xFF:
            raise ImageError("this JPEG is malformed and cannot be embedded")

        marker = data[position + 1]
        position += 2
        if marker in _JPEG_STANDALONE or marker == 0xFF:
            continue

        if position + 2 > len(data):
            break
        (length,) = struct.unpack(">H", data[position : position + 2])

        # APP14/Adobe: its presence is what says a 4-component scan is stored
        # INVERTED. Without the `/Decode` array below such a JPEG renders as a
        # photographic negative, which is unmistakable and unmistakably ours.
        if marker == 0xEE and data[position + 2 : position + 7] == b"Adobe":
            adobe = True

        if marker == 0xC2:
            raise ImageError(
                "this is a progressive JPEG. Not every PDF reader decodes one, and "
                "an invoice has to open everywhere - re-export it as a baseline "
                "JPEG, or as a PNG."
            )

        if marker in _JPEG_SOF:
            precision, height, width, components = struct.unpack(
                ">BHHB", data[position + 2 : position + 8]
            )
            colour_space = _JPEG_COLOUR_SPACES.get(components)
            if colour_space is None:
                raise ImageError(
                    f"this JPEG has {components} colour components, which is not "
                    f"greyscale, RGB or CMYK. Re-export it as RGB."
                )
            if width <= 0 or height <= 0:
                raise ImageError("this JPEG reports no extent")

            return Image(
                width=width,
                height=height,
                data=data,
                filter_name=b"DCTDecode",
                colour_space=colour_space,
                bits=precision,
                decode=b"[1 0 1 0 1 0 1 0]" if adobe and components == 4 else None,
            )

        position += length

    raise ImageError("this JPEG has no frame header and cannot be embedded")


# --- PNG ---------------------------------------------------------------------

#: Channels per pixel, by PNG colour type. Types 4 and 6 carry the alpha this
#: writer has to split out; type 3 is one index per pixel into a palette.
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _png(data: bytes) -> Image:
    header, palette, idat, transparency = _png_chunks(data)
    width, height, bits, colour_type, _, _, interlace = struct.unpack(">IIBBBBB", header)

    if interlace:
        raise ImageError(
            "this PNG is interlaced (Adam7). Re-save it without interlacing - most "
            "export dialogues call the option 'interlaced' or 'progressive'."
        )
    if colour_type not in _PNG_CHANNELS:
        raise ImageError(f"this PNG has an unknown colour type ({colour_type})")
    if not idat:
        raise ImageError("this PNG carries no image data")
    if width <= 0 or height <= 0:
        raise ImageError("this PNG reports no extent")

    channels = _PNG_CHANNELS[colour_type]

    if colour_type in (4, 6):
        # The alpha path: the only one that decodes. See the section header.
        return _png_with_alpha(
            idat,
            width=width,
            height=height,
            bits=bits,
            channels=channels,
        )

    if colour_type == 3:
        if not palette:
            raise ImageError("this PNG is indexed and carries no palette")
        colour_space = b"[/Indexed /DeviceRGB %d <%s>]" % (
            len(palette) // 3 - 1,
            palette.hex().encode("ascii"),
        )
    else:
        colour_space = b"/DeviceGray" if colour_type == 0 else b"/DeviceRGB"

    # The pass-through path: PDF's Predictor 15 IS PNG's filtering, so the
    # IDAT stream is handed over exactly as it arrived.
    return Image(
        width=width,
        height=height,
        data=idat,
        filter_name=b"FlateDecode",
        colour_space=colour_space,
        bits=bits,
        decode_parms=(
            b"<< /Predictor 15 /Colors %d /BitsPerComponent %d /Columns %d >>"
            % (channels, bits, width)
        ),
        mask=_png_index_mask(transparency) if colour_type == 3 and transparency else None,
    )


def _png_chunks(data: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """IHDR, PLTE, the concatenated IDATs, and tRNS.

    IDAT is split across chunks at the encoder's whim and the pieces are one
    zlib stream, so they are joined rather than read individually.
    """
    header = palette = transparency = b""
    idat = bytearray()
    position = 8

    while position + 8 <= len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        position += 12 + length  # 4 length + 4 type + body + 4 CRC

        if kind == b"IHDR":
            header = body
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            idat.extend(body)
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IEND":
            break

    if len(header) < 13:
        raise ImageError("this PNG has no header chunk and cannot be embedded")
    return header, palette, bytes(idat), transparency


def _png_index_mask(transparency: bytes) -> bytes:
    """`/Mask` ranges for an indexed PNG whose transparency is all-or-nothing.

    The common "transparent background" export: one palette entry is invisible
    and the rest are opaque. PDF expresses that as index ranges to drop, so it
    costs no decoding at all.

    Partial alpha per index is refused rather than approximated. Rounding a
    half-transparent palette entry to visible or invisible changes what the
    logo looks like, and doing it silently is how somebody's soft drop shadow
    becomes a grey box on every invoice they send.
    """
    if any(0 < value < 255 for value in transparency):
        raise ImageError(
            "this PNG uses partial transparency in its palette, which cannot be "
            "embedded directly. Re-export it as a full-colour PNG with an alpha "
            "channel (PNG-24 rather than PNG-8)."
        )

    ranges = b"".join(
        b"%d %d " % (index, index) for index, value in enumerate(transparency) if value == 0
    )
    return b"[%s]" % ranges.strip() if ranges else b""


def _png_with_alpha(idat: bytes, *, width: int, height: int, bits: int, channels: int) -> Image:
    """Split an RGBA or grey+alpha PNG into a colour image and an `/SMask`.

    The only path here that looks at a pixel, and it exists because PDF has no
    interleaved alpha. Both halves are re-compressed WITHOUT a predictor -
    un-filtering then re-filtering would cost more than it saves on a logo, and
    a flat stream has one fewer thing to get wrong.

    Deterministic: `zlib.compress` at a fixed level over fixed input, so the
    same logo produces the same bytes on every render (see the module
    docstring's note on FR-TPL-017).
    """
    if bits not in (8, 16):
        raise ImageError(
            f"this PNG has {bits}-bit samples with an alpha channel, which PNG "
            f"itself does not permit. The file may be damaged."
        )

    sample = bits // 8
    colour_channels = channels - 1
    stride = width * channels * sample
    bpp = channels * sample

    try:
        raw = zlib.decompress(idat)
    except zlib.error as exc:
        raise ImageError("this PNG's image data could not be read; it may be damaged") from exc

    expected = (stride + 1) * height
    if len(raw) < expected:
        raise ImageError("this PNG is truncated and cannot be embedded")

    flat = _unfilter(raw, height=height, stride=stride, bpp=bpp)

    colour = bytearray(width * height * colour_channels * sample)
    alpha = bytearray(width * height * sample)
    colour_at = alpha_at = 0

    for start in range(0, len(flat), bpp):
        pixel = flat[start : start + bpp]
        split = colour_channels * sample
        colour[colour_at : colour_at + split] = pixel[:split]
        colour_at += split
        alpha[alpha_at : alpha_at + sample] = pixel[split:]
        alpha_at += sample

    return Image(
        width=width,
        height=height,
        data=zlib.compress(bytes(colour), 9),
        filter_name=b"FlateDecode",
        colour_space=b"/DeviceGray" if colour_channels == 1 else b"/DeviceRGB",
        bits=bits,
        smask=Image(
            width=width,
            height=height,
            data=zlib.compress(bytes(alpha), 9),
            filter_name=b"FlateDecode",
            colour_space=b"/DeviceGray",
            bits=bits,
        ),
    )


def _unfilter(raw: bytes, *, height: int, stride: int, bpp: int) -> bytes:
    """PNG's five scanline filters, reversed.

    Straight from the PNG specification (RFC 2083 §6). Each scanline is
    prefixed with its filter type and is decoded against the RECONSTRUCTED
    line above it, not the raw one - which is the detail that turns a correct
    implementation into a smeared image when it is missed.
    """
    out = bytearray()
    previous = bytearray(stride)
    position = 0

    for _ in range(height):
        filter_type = raw[position]
        position += 1
        line = bytearray(raw[position : position + stride])
        position += stride

        if filter_type == 0:  # None
            pass
        elif filter_type == 1:  # Sub
            for index in range(bpp, stride):
                line[index] = (line[index] + line[index - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif filter_type == 3:  # Average
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                above = previous[index]
                upper_left = previous[index - bpp] if index >= bpp else 0
                estimate = left + above - upper_left
                da, db, dc = (
                    abs(estimate - left),
                    abs(estimate - above),
                    abs(estimate - upper_left),
                )
                if da <= db and da <= dc:
                    nearest = left
                elif db <= dc:
                    nearest = above
                else:
                    nearest = upper_left
                line[index] = (line[index] + nearest) & 0xFF
        else:
            raise ImageError(
                f"this PNG uses an unknown scanline filter ({filter_type}); it may be damaged"
            )

        out.extend(line)
        previous = line

    return bytes(out)


def _image_body(image: Image, *, smask_number: int | None) -> bytes:
    parts = [
        b"<< /Type /XObject /Subtype /Image",
        b"/Width %d /Height %d" % (image.width, image.height),
        b"/ColorSpace %s" % image.colour_space,
        b"/BitsPerComponent %d" % image.bits,
        b"/Filter /%s" % image.filter_name,
    ]
    if image.decode_parms:
        parts.append(b"/DecodeParms %s" % image.decode_parms)
    if image.decode:
        parts.append(b"/Decode %s" % image.decode)
    if image.mask:
        parts.append(b"/Mask %s" % image.mask)
    if smask_number is not None:
        parts.append(b"/SMask %d 0 R" % smask_number)
    parts.append(b"/Length %d >>" % len(image.data))

    return b" ".join(parts) + b"\nstream\n" + image.data + b"\nendstream"


# =============================================================================
# Tagged structure - FR-TPL-016
# =============================================================================


@dataclass
class _StructNode:
    """One node of the structure tree - a LEAF (`mcid` set) wrapping one
    marked-content item on one page, or a GROUP (`mcid` None) whose meaning is
    its children.

    Internal to this module. `api.invoicing.rendering` never sees one; it only
    ever hands `Page.at` / `Page.place_image` a `StructureTag`, and this is
    what `render_pdf` builds from the result.
    """

    tag: StructureTag
    children: list[_StructNode] = field(default_factory=list)
    page_index: int | None = None
    mcid: int | None = None
    #: Figures only (PDF/A 6.8.3 / FR-TPL-016).
    alt: str | None = None


def _build_structure(pages: list[Page]) -> tuple[_StructNode, dict[int, int]]:
    """The structure tree, and which MCID `_content_stream` must use for each
    tagged item.

    Walked in the SAME order `_content_stream` draws - images, then text runs,
    per page, skipping empty text runs exactly as that function does. The two
    must never diverge: a structure tree built in one order describing a
    content stream written in another produces a document whose ACCESSIBLE
    reading order and VISIBLE one disagree, which is worse than no structure
    at all - it tells a screen reader something false rather than nothing.

    --- Table grouping ---

    A cell (`TABLE_HEADER` / `TABLE_CELL`) joins the currently open table,
    starting a new `TR` whenever `Text.row` changes and a new `Table` if none
    is open. Any NON-cell text run closes the open table, so two separate
    tables on one page - the line-item table and, if a business exports one, a
    payment schedule - are never merged into one.

    An IMAGE deliberately does NOT close an open table. Images are drawn first
    on every page (draw order, not reading order - see `Page.place_image`'s
    docstring), so a repeated header logo on page two of a multi-page invoice
    would otherwise split what should be one continuous Table spanning both
    pages (FR-TPL-010's repeating headers) into two, right after the very
    column headers meant to repeat.

    An ARTIFACT text run (`Text.is_artifact`) does not close it either, for
    the same reason and a sharper example: a running footer is drawn on the
    OLD page right before the break to a new one, so it sits in `page.texts`
    AFTER that page's last line item and BEFORE the new page's repeated
    header. Treating it as ordinary paragraph content - which is what it
    would be under the "any non-cell run closes the table" rule - would end
    the Table at every single page break. It is skipped here exactly like an
    empty run: no MCID, no structure node, and no effect on `table`/`row` at
    all.
    """
    document = _StructNode(tag=StructureTag.DOCUMENT)
    mcid_of: dict[int, int] = {}

    table: _StructNode | None = None
    row: _StructNode | None = None
    row_number: int | None = None

    for page_index, page in enumerate(pages):
        mcid = 0

        for placement in page.images:
            document.children.append(
                _StructNode(
                    tag=StructureTag.FIGURE,
                    page_index=page_index,
                    mcid=mcid,
                    alt=placement.alt,
                )
            )
            mcid_of[id(placement)] = mcid
            mcid += 1

        for text in page.texts:
            if not text.value or text.is_artifact:
                # `_content_stream` makes no TAGGED mark for either (an empty
                # run gets none at all; an artifact gets `/Artifact` instead
                # of `/Tag << /MCID n >>`), so neither consumes an MCID here -
                # a gap would desynchronise every mcid after it from what the
                # content stream actually emits. See the docstring above for
                # why artifacts additionally must not touch `table`/`row`.
                continue

            leaf = _StructNode(tag=text.tag, page_index=page_index, mcid=mcid)
            mcid_of[id(text)] = mcid
            mcid += 1

            if text.tag.is_cell:
                if table is None:
                    table = _StructNode(tag=StructureTag.TABLE)
                    document.children.append(table)
                    row = None
                assert text.row is not None  # guaranteed by Text.__post_init__
                if row is None or text.row != row_number:
                    row = _StructNode(tag=StructureTag.TABLE_ROW)
                    table.children.append(row)
                    row_number = text.row
                row.children.append(leaf)
            else:
                table = None
                row = None
                row_number = None
                document.children.append(leaf)

    return document, mcid_of


def _flatten_structure(
    document: _StructNode,
) -> tuple[list[_StructNode], dict[int, _StructNode | None]]:
    """Pre-order traversal - the node itself, then each child recursively -
    alongside a map from every node to its parent.

    Pre-order is what lets `render_pdf` assign object numbers by POSITION in
    the result: the Document root always comes first, so it always gets the
    first structure object number, and the numbering arithmetic never has to
    special-case it.

    `parent_of[id(document)]` is `None` - the Document root's parent is the
    StructTreeRoot, which is not itself a `_StructNode`, and `render_pdf`
    substitutes the fixed object number for that one case.
    """
    ordered: list[_StructNode] = []
    parent_of: dict[int, _StructNode | None] = {}

    def visit(node: _StructNode, parent: _StructNode | None) -> None:
        ordered.append(node)
        parent_of[id(node)] = parent
        for child in node.children:
            visit(child, node)

    visit(document, None)
    return ordered, parent_of


def _show_text_operand(text: Text, embedded_by_name: dict[str, EmbeddedFont]) -> bytes:
    """The `Tj` operand for one run: a WinAnsi literal string `(...)` for a
    base-14 face, or an `Identity-H` hex string `<...>` of 2-byte CIDs for an
    embedded one.

    Which of the two applies is decided entirely by whether `text.font` names
    an `EmbeddedFont` - the same resource name a caller set on `Text.font`,
    never a separate flag to keep in sync with it.
    """
    embedded = embedded_by_name.get(text.font)
    if embedded is None:
        return b"(%s)" % _escape(text.value)
    return b"<%s>" % embedded.encode(text.value).hex().encode("ascii")


def _content_stream(
    page: Page,
    names: dict[bytes, bytes],
    mcid_of: dict[int, int],
    embedded_by_name: dict[str, EmbeddedFont],
) -> bytes:
    """One page's marks, as PDF content operators.

    Every mark is either TAGGED (`/Tag << /MCID n >> BDC ... EMC`, linking it
    to the structure element `_build_structure` made for it) or explicitly an
    ARTIFACT (`/Artifact BDC ... EMC`, for a rule, which carries no meaning a
    reader should announce). Leaving a mark as neither is the "unmarked
    content" gap a structure validator flags; tagging a decorative rule as
    real content would be the opposite mistake, reading it aloud between every
    section.

    Images are drawn FIRST so nothing a logo overlaps is hidden behind it - a
    watermark or a tinted header band is the case that makes the order matter,
    and discovering it later means finding out from a customer. Filled
    rectangles (`template.colors.background`'s panels) are drawn SECOND, ahead
    of rules and text, for the same reason: a coloured band behind a heading
    must not paint over the heading.
    `_build_structure` assigns MCIDs in this same order; see its docstring for
    why the two must never diverge - rectangles carry no MCID at all (like
    rules), so their presence here does not shift any of it.
    """
    parts: list[bytes] = []

    for placement in page.images:
        mcid = mcid_of[id(placement)]
        parts.append(b"/Figure << /MCID %d >> BDC\n" % mcid)
        # `q`/`Q` bracket the transformation so a scaled image does not scale
        # everything drawn after it. `cm` maps PDF's unit square onto the
        # display rectangle - which is why the pixel dimensions play no part.
        parts.append(
            b"q %.2f 0 0 %.2f %.2f %.2f cm /%s Do Q\n"
            % (
                placement.width,
                placement.height,
                placement.x,
                placement.y,
                names[placement.image.digest],
            )
        )
        parts.append(b"EMC\n")

    for rect in page.rects:
        parts.append(b"/Artifact BDC\n")
        parts.append(
            b"q %s rg %.2f %.2f %.2f %.2f re f Q\n"
            % (rect.color._operand(), rect.x, rect.y, rect.width, rect.height)
        )
        parts.append(b"EMC\n")

    for line in page.lines:
        parts.append(b"/Artifact BDC\n")
        prefix = b"q %s RG " % line.color._operand() if line.color is not None else b""
        suffix = b" Q" if line.color is not None else b""
        parts.append(
            b"%s%.2f w %.2f %.2f m %.2f %.2f l S%s\n"
            % (prefix, line.width, line.x1, line.y, line.x2, line.y, suffix)
        )
        parts.append(b"EMC\n")

    for text in page.texts:
        if not text.value:
            continue

        operand = _show_text_operand(text, embedded_by_name)
        show = b"BT /%s %.2f Tf %.2f %.2f Td %s Tj ET\n" % (
            text.font.encode("ascii"),
            text.size,
            text.x,
            text.y,
            operand,
        )
        # `q`/`Q` bracket any non-default graphics-state change - a fill
        # colour (`rg`) or a character spacing (`Tc`, FR-TPL-003) - so it
        # cannot leak into runs after it: both set GENERAL graphics state,
        # which otherwise persists across every `BT`/`ET` block that follows,
        # not just this one. Neither is emitted at all when unset, which is
        # what keeps a run requesting neither byte-identical to before this
        # (and before Color) existed.
        state: list[bytes] = []
        if text.color is not None:
            state.append(b"%s rg" % text.color._operand())
        if text.letter_spacing:
            state.append(b"%.2f Tc" % text.letter_spacing)
        run = b"q %s\n%sQ\n" % (b" ".join(state), show) if state else show
        if text.is_artifact:
            # A running header/footer or a page number: repeated boilerplate,
            # not part of the reading flow, and not something `_build_structure`
            # assigned an MCID to - see its docstring for why closing an
            # open table on this specific mark would be the wrong outcome.
            parts.append(b"/Artifact BDC\n")
            parts.append(run)
            parts.append(b"EMC\n")
            continue

        mcid = mcid_of[id(text)]
        parts.append(b"/%s << /MCID %d >> BDC\n" % (text.tag.value.encode("ascii"), mcid))
        parts.append(run)
        parts.append(b"EMC\n")
    return b"".join(parts)


#: Human-readable labels for the two built-in faces, for the D5 message
#: `assert_conformance` builds when text is still drawn with them - see
#: `render_pdf`'s `unembedded_fonts` computation.
_BASE14_LABELS: dict[str, str] = {
    Font.REGULAR: "Helvetica (base-14)",
    Font.BOLD: "Helvetica-Bold (base-14)",
}


def render_pdf(
    pages: list[Page],
    *,
    title: str,
    language: str = "nl",
    conformance: Conformance = Conformance.NONE,
    icc_profile: bytes | None = None,
    embedded_fonts: Sequence[EmbeddedFont] = (),
) -> bytes:
    """Serialise pages into a PDF file - tagged, Unicode-mapped, and honest
    about whether it is PDF/A.

    --- `embedded_fonts`: additive, and what makes `fonts_embedded` real ---

    Base-14 `Font.REGULAR`/`Font.BOLD` keep working exactly as before for any
    caller that supplies none - this parameter is additive, not a mode switch.
    A `Text` run whose `font` names one of `embedded_fonts`'s own
    `resource_name`s is drawn as an `Identity-H` CID string against a real
    `Type0`/`CIDFontType2` font instead of a WinAnsi literal string against a
    base-14 face (see `_show_text_operand`). Whether the RESULT may claim
    `fonts_embedded=True` to `assert_conformance` is computed genuinely from
    what the document actually drew, not from whether `embedded_fonts` is
    non-empty: a document mixing an embedded heading font with base-14 body
    text still has base-14 text in it, and PDF/A forbids relying on ANY
    reader-supplied face, not most of them.

    --- FR-TPL-016: every file is tagged, unconditionally ---

    Structure, alternative text and a ToUnicode CMap are built and written
    regardless of `conformance` - they are what makes the text selectable and
    the reading order announced correctly, and FR-TPL-016 asks for that of
    every invoice, archival or not.

    --- FR-TPL-015: `conformance` is a request, and it can be refused ---

    `assert_conformance` (api.invoicing.pdfa) runs FIRST, before anything is
    built, and raises naming exactly what is missing if the claim would be
    false. `fonts_embedded` is computed genuinely below (see `embedded_fonts`
    above) rather than hardcoded: without a deployment-supplied font this
    still raises exactly as before, but now because the check is honest, not
    because it is fixed. `icc_profile` is embedded whenever one is supplied,
    independently of whether `conformance` itself is achievable - which is
    what lets that half of the PDF/A mechanism be exercised and tested even
    when the other half is not.

    --- The object layout ---

        1  Catalog
        2  Pages
        3  Helvetica            (both fonts share one ToUnicode CMap: object 7)
        4  Helvetica-Bold
        5  Info
        6  Metadata             (XMP stream)
        7  ToUnicode CMap
        8  StructTreeRoot
        9..           the structure tree, Document first, in pre-order
        then          OutputIntent and its ICC profile stream, if one was given
        then          5 objects per embedded font, in `embedded_fonts` order:
                       Type0, CIDFontType2, FontDescriptor, FontFile2 stream,
                       CID-keyed ToUnicode CMap
        then          one object per distinct image, plus one per alpha mask
        then          one Page and one Contents per page

    Structure comes before images because computing it costs nothing that
    depends on object numbers - unlike images, whose count depends on what a
    page actually draws - so putting it first keeps the numbering arithmetic a
    straight line rather than a case split. Embedded fonts are placed after
    the OutputIntent for the same reason: their count depends only on
    `embedded_fonts`, known before a single `add()` call, so they extend the
    straight line rather than breaking it.

    `Info` carries no `CreationDate` and no `Producer` version, and the XMP
    packet carries no `xmp:CreateDate`. All three would be the only
    non-deterministic bytes in the file, and determinism is worth more here
    than metadata nobody reads: two renderings of one invoice produce
    identical bytes, so the archive's content hash (FR-DOC-001) is a property
    of the INVOICE rather than of the moment it was rendered. The trailer's
    `/ID` follows the same rule - a hash of the document's own object bytes,
    never the clock or a random source.
    """
    if not pages:
        raise ValueError("a PDF needs at least one page")

    embedded_by_name: dict[str, EmbeddedFont] = {}
    for embedded in embedded_fonts:
        name = embedded.resource_name.decode("ascii")
        if name in (Font.REGULAR, Font.BOLD):
            raise ValueError(
                f"an embedded font cannot use resource name {name!r}; that name "
                f"is reserved for the base-14 faces."
            )
        if name in embedded_by_name:
            raise ValueError(f"two embedded fonts both claim resource name {name!r}")
        embedded_by_name[name] = embedded

    # Genuinely computed, not asserted: which font every non-empty Text run on
    # the document actually draws with, and which of those are NOT one of
    # `embedded_fonts`. Base-14 fonts never count, even if nothing else is
    # drawn with them - PDF/A forbids relying on a reader-supplied face at
    # all, so their mere presence in `Font` is not itself a gap, only USING
    # one is.
    used_fonts = {text.font for page in pages for text in page.texts if text.value}
    unembedded_used_fonts = used_fonts - set(embedded_by_name)
    fonts_embedded = bool(used_fonts) and not unembedded_used_fonts
    unembedded_labels = frozenset(
        _BASE14_LABELS.get(name, f"font resource /{name}") for name in unembedded_used_fonts
    )

    # Before anything is built: a false claim is refused at the door, not
    # discovered by a validator after the fact.
    assert_conformance(
        conformance,
        fonts_embedded=fonts_embedded,
        icc_profile=icc_profile,
        unembedded_fonts=unembedded_labels,
    )

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    # --- structure, built first so its size is known before anything is
    # numbered ---
    document, mcid_of = _build_structure(pages)
    struct_nodes, parent_of = _flatten_structure(document)
    struct_number: dict[int, int] = {id(node): 9 + index for index, node in enumerate(struct_nodes)}

    # --- distinct images, in first-use order so the file's layout is a
    # function of the document rather than of dictionary ordering ---
    distinct: list[Image] = []
    seen: set[bytes] = set()
    for page in pages:
        for placement in page.images:
            if placement.image.digest not in seen:
                seen.add(placement.image.digest)
                distinct.append(placement.image)
    # Every image is one object, and one more if it carries an alpha mask.
    image_object_count = sum(2 if image.smask else 1 for image in distinct)

    # --- the full numbering plan, computed before a single add() call - the
    # same arithmetic-then-emit shape the image scheme already used, extended
    # to cover structure and the optional OutputIntent ---
    first_struct_object = 9
    first_after_struct = first_struct_object + len(struct_nodes)
    has_icc = icc_profile is not None
    output_intent_number = first_after_struct if has_icc else None
    icc_stream_number = first_after_struct + 1 if has_icc else None
    first_embedded_font_object = first_after_struct + (2 if has_icc else 0)
    # Five objects per embedded font - see the object-layout table above.
    embedded_font_numbers = [
        first_embedded_font_object + index * 5 for index in range(len(embedded_fonts))
    ]
    first_image_object = first_embedded_font_object + 5 * len(embedded_fonts)
    first_page_object = first_image_object + image_object_count
    page_object_numbers = [first_page_object + index * 2 for index in range(len(pages))]
    font_resource_entries = [b"/F1 3 0 R", b"/F2 4 0 R"] + [
        b"/%s %d 0 R" % (embedded.resource_name, type0_number)
        for embedded, type0_number in zip(embedded_fonts, embedded_font_numbers, strict=True)
    ]

    output_intents = (
        b" /OutputIntents [%d 0 R]" % output_intent_number
        if output_intent_number is not None
        else b""
    )
    add(
        b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 8 0 R "
        b"/MarkInfo << /Marked true >> /Lang (%s) /Metadata 6 0 R%s >>"
        # `_escape`, not a bare `.encode` - `language` is a public parameter of
        # this function and every OTHER string written as a PDF literal here
        # (title, alt text) gets the same treatment. A BCP 47 tag is always
        # plain ASCII in practice, so this costs nothing for the one real
        # caller; it is what stops a future caller's unescaped paren from
        # corrupting every operator that follows it in the Catalog.
        % (_escape(language), output_intents)
    )
    add(
        b"<< /Type /Pages /Count %d /Kids [%s] >>"
        % (
            len(pages),
            b" ".join(b"%d 0 R" % number for number in page_object_numbers),
        )
    )
    add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding /ToUnicode 7 0 R >>"
    )
    add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding /ToUnicode 7 0 R >>"
    )
    add(b"<< /Title (%s) >>" % _escape(title))

    metadata = xmp_packet(title=title, language=language, conformance=conformance)
    add(
        b"<< /Type /Metadata /Subtype /XML /Length %d >>\nstream\n%s\nendstream"
        % (len(metadata), metadata)
    )

    cmap = to_unicode_cmap(_winansi_to_unicode())
    add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap))

    # --- StructTreeRoot. /ParentTree links each page's MCIDs back to the
    # structure elements that own them - the mechanism a page's /StructParents
    # entry addresses into. RoleMap is omitted: every tag this writer uses
    # (Document, H1, H2, P, Table, TR, TH, TD, Figure) is already a standard
    # PDF structure type, so there is nothing to map to one.
    by_page: dict[int, dict[int, int]] = {}
    for node in struct_nodes:
        if node.page_index is not None and node.mcid is not None:
            by_page.setdefault(node.page_index, {})[node.mcid] = struct_number[id(node)]

    nums: list[bytes] = []
    for page_index in range(len(pages)):
        page_mcids = by_page.get(page_index, {})
        refs = b" ".join(b"%d 0 R" % page_mcids[mcid] for mcid in sorted(page_mcids))
        nums.append(b"%d [%s]" % (page_index, refs))

    add(
        b"<< /Type /StructTreeRoot /K [%d 0 R] "
        b"/ParentTree << /Nums [%s] >> /ParentTreeNextKey %d >>"
        % (struct_number[id(document)], b" ".join(nums), len(pages))
    )

    # --- the structure tree itself, in the same pre-order the numbering above
    # assumes ---
    for node in struct_nodes:
        parent = parent_of[id(node)]
        # A None parent means the STRUCTURE ROOT (object 8), not another
        # element - `_flatten_structure`'s contract for the Document node.
        parent_number = 8 if parent is None else struct_number[id(parent)]

        if node.mcid is not None:
            # A leaf wrapping exactly one marked-content item: /K may be the
            # bare MCID integer rather than a marked-content reference
            # dictionary, which ISO 32000 7.9.4 permits for precisely this
            # case and which is what keeps a P or H1 element to one line.
            k_entry = b"%d" % node.mcid
        else:
            k_entry = b"[%s]" % b" ".join(
                b"%d 0 R" % struct_number[id(child)] for child in node.children
            )

        body = b"<< /Type /StructElem /S /%s /P %d 0 R" % (
            node.tag.value.encode("ascii"),
            parent_number,
        )
        if node.page_index is not None:
            body += b" /Pg %d 0 R" % page_object_numbers[node.page_index]
        if node.alt is not None:
            body += b" /Alt (%s)" % _escape(node.alt)
        body += b" /K %s >>" % k_entry
        add(body)

    # --- OutputIntent, only when this document was given a profile to claim
    # one with. See the module docstring for why the bytes are supplied rather
    # than generated. ---
    if icc_profile is not None:
        assert output_intent_number is not None and icc_stream_number is not None
        add(output_intent(icc_object_number=icc_stream_number))
        icc_stream = zlib.compress(icc_profile, 9)
        add(
            b"<< /N 3 /Alternate /DeviceRGB /Filter /FlateDecode /Length %d >>"
            b"\nstream\n%s\nendstream" % (len(icc_stream), icc_stream)
        )

    # --- embedded fonts: Type0, CIDFontType2, FontDescriptor, FontFile2 and a
    # CID-keyed ToUnicode CMap per font, in `embedded_fonts` order. ---
    for font_index, embedded in enumerate(embedded_fonts):
        type0_number = embedded_font_numbers[font_index]
        cidfont_number = type0_number + 1
        descriptor_number = type0_number + 2
        fontfile_number = type0_number + 3
        tounicode_number = type0_number + 4

        widths = b"[%s]" % b" ".join(
            b"%d [%d]" % (cid, width) for cid, width in sorted(embedded.widths.items())
        )
        add(
            b"<< /Type /Font /Subtype /Type0 /BaseFont /%s /Encoding /Identity-H "
            b"/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>"
            % (embedded.base_font_name, cidfont_number, tounicode_number)
        )
        add(
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /%s "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            b"/FontDescriptor %d 0 R /CIDToGIDMap /Identity /DW %d /W %s >>"
            % (embedded.base_font_name, descriptor_number, embedded.default_width, widths)
        )
        add(
            b"<< /Type /FontDescriptor /FontName /%s /Flags %d "
            b"/FontBBox [%d %d %d %d] /ItalicAngle %g /Ascent %d /Descent %d "
            b"/CapHeight %d /StemV %d /FontFile2 %d 0 R >>"
            % (
                embedded.base_font_name,
                embedded.flags,
                *embedded.font_bbox,
                embedded.italic_angle,
                embedded.ascent,
                embedded.descent,
                embedded.cap_height,
                embedded.stem_v,
                fontfile_number,
            )
        )
        font_program_stream = zlib.compress(embedded.font_program, 9)
        add(
            b"<< /Length1 %d /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream"
            % (len(embedded.font_program), len(font_program_stream), font_program_stream)
        )
        cid_cmap = to_unicode_cmap_cid(embedded.cid_to_unicode)
        add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cid_cmap), cid_cmap))

    # --- images: unchanged from before, just shifted by everything above ---
    names: dict[bytes, bytes] = {}
    numbers: dict[bytes, int] = {}
    for index, image in enumerate(distinct):
        smask_number = add(_image_body(image.smask, smask_number=None)) if image.smask else None
        names[image.digest] = b"Im%d" % index
        numbers[image.digest] = add(_image_body(image, smask_number=smask_number))

    # --- pages ---
    font_resources = b" ".join(font_resource_entries)
    for page_index, page in enumerate(pages):
        stream = _content_stream(page, names, mcid_of, embedded_by_name)
        contents_number = len(objects) + 2

        # Only the images this page actually uses. A Resources dictionary
        # naming every image in the document would make each page depend on
        # objects it never draws, which is legal and is the kind of thing that
        # keeps a stale XObject alive after the page using it was removed.
        used: list[bytes] = []
        for placement in page.images:
            entry = b"/%s %d 0 R" % (
                names[placement.image.digest],
                numbers[placement.image.digest],
            )
            if entry not in used:
                used.append(entry)
        xobjects = b" /XObject << %s >>" % b" ".join(used) if used else b""

        add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << %s >>%s >> "
            # /StructParents links this page back to its slice of the
            # /ParentTree above. /Tabs /S says tab order should follow
            # structure order, which only means anything once it exists.
            b"/Contents %d 0 R /StructParents %d /Tabs /S >>"
            % (
                page.page_width,
                page.page_height,
                font_resources,
                xobjects,
                contents_number,
                page_index,
            )
        )
        add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))

    out = bytearray(b"%PDF-1.7\n")
    # A binary comment, as the spec recommends, so a transfer that mangles line
    # endings is detectable rather than producing a subtly broken file.
    out.extend(b"%\xe2\xe3\xcf\xd3\n")

    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(b"%d 0 obj\n" % number)
        out.extend(body)
        out.extend(b"\nendobj\n")

    xref_offset = len(out)
    out.extend(b"xref\n0 %d\n" % (len(objects) + 1))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        out.extend(b"%010d 00000 n \n" % offset)

    # PDF/A 6.1.3's file identifier, and a useful one regardless: a hash of
    # every object's bytes, so the SAME invoice produces the SAME id and two
    # different invoices do not collide. Sixteen bytes to match the
    # conventional MD5-shaped /ID every reader expects, without using MD5.
    file_id = hashlib.sha256(b"".join(objects)).digest()[:16].hex().encode("ascii")
    out.extend(
        b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R /ID [<%s> <%s>] >>"
        b"\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, file_id, file_id, xref_offset)
    )
    return bytes(out)
