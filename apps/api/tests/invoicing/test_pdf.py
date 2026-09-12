"""api.invoicing.pdf - the mechanics behind FR-TPL-017's stored rendering.

These are structural tests over bytes, which is unusual and is the point: the
output is a file a customer receives and CMP-001 keeps for seven years, so
"does it open" is not a thing to find out later. There is no PDF parser in this
project's dependencies, so the assertions read the format directly - which is
tractable because the writer emits so little of it.
"""

from __future__ import annotations

import re

import pytest

from api.invoicing.pdf import (
    A4_HEIGHT,
    A4_WIDTH,
    LETTER_HEIGHT,
    LETTER_WIDTH,
    Color,
    Font,
    Page,
    Text,
    render_pdf,
    text_width,
    wrap,
)
from api.invoicing.pdfa import Conformance
from api.invoicing.truetype import build_embedded_font, parse_font
from tests.invoicing.support.synthetic_font import CODEPOINTS, build_synthetic_font

_SYNTHETIC_FONT = build_synthetic_font()
FAKE_ICC = b"fake-icc-profile-bytes-not-a-real-profile"


def _embedded(codepoints: set[int], resource_name: bytes = b"F3"):  # type: ignore[no-untyped-def]
    font = parse_font(_SYNTHETIC_FONT)
    return build_embedded_font(
        font, resource_name=resource_name, base_font_name=b"SyntheticTest", codepoints=codepoints
    )


def _one_page(value: str = "Factuur 2026-1") -> bytes:
    page = Page()
    page.at(x=56, top=56, value=value)
    return render_pdf([page], title="Factuur")


def _content_stream_of(data: bytes) -> bytes:
    """The page's CONTENTS stream, not just any stream.

    A rendered file now carries an XMP Metadata stream and a ToUnicode CMap
    stream ahead of every page (FR-TPL-016), so "the first stream in the
    file" is no longer the page content. For a single-page document the
    Contents stream is always the LAST one written - see `render_pdf`'s
    object layout.
    """
    return re.findall(rb"stream\n(.*?)\nendstream", data, re.S)[-1]


# --- file structure ----------------------------------------------------------


def test_the_file_has_a_header_and_a_trailer() -> None:
    data = _one_page()

    assert data.startswith(b"%PDF-1.7\n")
    assert data.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in data
    assert b"startxref" in data


def test_the_binary_comment_is_present() -> None:
    """The spec recommends it so a transfer that mangles line endings produces
    a detectably broken file rather than a subtly broken one.
    """
    assert b"%\xe2\xe3\xcf\xd3" in _one_page()


def test_every_xref_offset_points_at_its_object() -> None:
    """The cross-reference table is what a reader uses to find objects. An
    offset that is out by one byte produces a file that opens in a forgiving
    viewer and fails in a strict one - which is exactly the failure that would
    be discovered by a customer rather than by us.
    """
    data = _one_page()

    start = int(re.search(rb"startxref\n(\d+)", data).group(1))
    table = data[start:]
    offsets = [int(m) for m in re.findall(rb"^(\d{10}) 00000 n", table, re.M)]

    assert offsets, "no xref entries"
    for number, offset in enumerate(offsets, start=1):
        assert data[offset:].startswith(b"%d 0 obj" % number), (
            f"xref entry {number} points at offset {offset}, which is not that object"
        )


def test_the_object_count_matches_the_trailer() -> None:
    data = _one_page()

    declared = int(re.search(rb"/Size (\d+)", data).group(1))
    actual = len(re.findall(rb"^\d+ 0 obj", data, re.M))

    # /Size counts the free object 0 as well.
    assert declared == actual + 1


def test_a4_is_the_media_box() -> None:
    assert b"/MediaBox [0 0 595 842]" in _one_page()
    assert (A4_WIDTH, A4_HEIGHT) == (595, 842)


def test_default_page_size_is_byte_identical_to_before_page_size_existed() -> None:
    """FR-TPL-010's additive promise, at the byte level: a `Page()` built with
    no `page_width`/`page_height` (the default) produces EXACTLY the bytes it
    did before those two fields existed - the same proof
    `test_existing_base14_only_rendering_is_byte_identical_with_the_new_parameter`
    gives for `embedded_fonts`.
    """
    with_default = _one_page()

    explicit_page = Page(page_width=A4_WIDTH, page_height=A4_HEIGHT)
    explicit_page.at(x=56, top=56, value="Factuur 2026-1")
    explicit_a4 = render_pdf([explicit_page], title="Factuur")

    assert with_default == explicit_a4


def test_letter_page_size_changes_the_media_box() -> None:
    page = Page(page_width=LETTER_WIDTH, page_height=LETTER_HEIGHT)
    page.at(x=56, top=56, value="Factuur 2026-1")
    data = render_pdf([page], title="Factuur")

    assert b"/MediaBox [0 0 612 792]" in data
    assert (LETTER_WIDTH, LETTER_HEIGHT) == (612, 792)


def test_a_letter_pages_own_top_to_bottom_flip_uses_its_own_height() -> None:
    """`Page.at`'s coordinate flip (`y = page_height - top`) must use THIS
    page's own height, not A4's - otherwise a Letter page (792pt, shorter
    than A4's 842pt) would place everything 50pt too high.
    """
    a4 = Page()
    a4.at(x=56, top=56, value="x")
    letter = Page(page_width=LETTER_WIDTH, page_height=LETTER_HEIGHT)
    letter.at(x=56, top=56, value="x")

    assert a4.texts[0].y == A4_HEIGHT - 56
    assert letter.texts[0].y == LETTER_HEIGHT - 56


def test_both_fonts_are_declared_with_winansi() -> None:
    """WinAnsiEncoding is what makes the euro sign and Dutch accented
    characters render. Without it a reader falls back to StandardEncoding,
    where 0x80 is not the euro.
    """
    data = _one_page()

    assert data.count(b"/Encoding /WinAnsiEncoding") == 2
    assert b"/BaseFont /Helvetica " in data or b"/BaseFont /Helvetica\n" in data
    assert b"/BaseFont /Helvetica-Bold" in data


# --- determinism -------------------------------------------------------------


def test_the_same_page_renders_to_the_same_bytes() -> None:
    """No clock, no random source. A small independent check on FR-TPL-017: a
    renderer whose output drifted between two runs over identical input could
    not be storing "the PDF as issued" in any meaningful sense, and the
    archive's content hash would be a property of the moment rather than of the
    invoice.
    """
    assert _one_page() == _one_page()


def test_no_creation_date_is_written() -> None:
    """The only bytes that would differ between two renderings."""
    data = _one_page()

    assert b"CreationDate" not in data
    assert b"ModDate" not in data


# --- escaping and encoding ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Bakker (Holding) B.V.",
        "De Vries \\ Zonen",
        "Parenthesis ) unbalanced",
        "Both ( and ) present",
    ],
)
def test_parentheses_and_backslashes_are_escaped(name: str) -> None:
    """An unescaped `)` terminates the string early and corrupts every operator
    after it. "Bakker (Holding) B.V." is an ordinary Dutch company name, so this
    is reachable with real data on the first day.
    """
    data = render_pdf([_page_with(name)], title="x")

    body = _content_stream_of(data)
    # Every literal paren in the payload is backslash-escaped; the delimiters
    # of the string operand are not.
    inner = re.search(rb"\((.*)\) Tj", body, re.S).group(1)
    for index, char in enumerate(inner):
        if char in b"()":
            assert index > 0 and inner[index - 1 : index] == b"\\", (
                f"unescaped {chr(char)} at {index} in {inner!r}"
            )


def test_the_euro_sign_becomes_the_winansi_byte() -> None:
    data = render_pdf([_page_with("€ 1.234,56")], title="x")
    assert b"\x80" in data


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Müller", b"M\xfcller"),
        ("Ångström", b"\xc5ngstr\xf6m"),
        ("Besançon", b"Besan\xe7on"),
    ],
)
def test_latin1_characters_survive(value: str, expected: bytes) -> None:
    """Dutch and German company names are full of these; losing one produces a
    name that looks like a typo nobody made.
    """
    assert expected in render_pdf([_page_with(value)], title="x")


def test_a_character_outside_winansi_is_transliterated_not_dropped() -> None:
    """`ł` has no WinAnsi byte. It becomes `l` rather than vanishing, because a
    name rendered slightly wrong is reportable and a name silently missing a
    letter is not.
    """
    data = render_pdf([_page_with("Kowałski")], title="x")

    assert b"Kowalski" in data
    assert b"Kowaski" not in data


def test_a_character_with_no_ascii_form_becomes_a_visible_marker() -> None:
    data = render_pdf([_page_with("Emoji 🙂 here")], title="x")
    assert b"Emoji ? here" in data


# --- FR-TPL-016 --------------------------------------------------------------


def test_text_below_nine_point_is_refused() -> None:
    """FR-TPL-016's minimum effective body size. Enforced at the type rather
    than trusted, because the way a layout breaks this rule is by shrinking a
    column to make it fit.
    """
    with pytest.raises(ValueError, match="9pt"):
        Text(x=0, y=0, value="too small", size=8.5)


def test_nine_point_exactly_is_allowed() -> None:
    assert Text(x=0, y=0, value="fine", size=9.0).size == 9.0


def test_the_text_is_real_text_and_not_an_image() -> None:
    """The half of FR-TPL-016 a minimal writer can deliver: selectable text,
    never a rendered image. `Tj` is the show-text operator.
    """
    data = _one_page("Selecteerbaar")

    assert b" Tj" in data
    assert b"/Image" not in data
    assert b"/DCTDecode" not in data


# --- coordinates -------------------------------------------------------------


def test_at_flips_top_down_coordinates_to_pdf_bottom_up() -> None:
    """The single most common way a generated PDF comes out upside down. The
    flip happens in `Page.at` and nowhere else.
    """
    page = Page()
    page.at(x=10, top=100, value="near the top")

    assert page.texts[0].y == A4_HEIGHT - 100


def test_a_rule_flips_the_same_way() -> None:
    page = Page()
    page.rule(x1=10, top=100, x2=200)

    assert page.lines[0].y == A4_HEIGHT - 100


# --- measurement and wrapping ------------------------------------------------


def test_width_scales_with_size() -> None:
    assert text_width("MMMM", 20.0) == pytest.approx(text_width("MMMM", 10.0) * 2)


def test_width_is_zero_for_empty_text() -> None:
    assert text_width("", 10.0) == 0


def test_wrap_breaks_on_spaces() -> None:
    lines = wrap("een twee drie vier vijf zes zeven acht", width=60.0, size=9.0)

    assert len(lines) > 1
    for line in lines:
        assert text_width(line, 9.0) <= 60.0 or " " not in line


def test_wrap_hard_breaks_a_word_longer_than_the_column() -> None:
    """A product code with no spaces must not run off the page and out of the
    printable area, where it would be absent from the document rather than
    merely ugly.
    """
    lines = wrap("A" * 200, width=50.0, size=9.0)

    assert len(lines) > 1
    for line in lines:
        assert text_width(line, 9.0) <= 50.0


def test_wrap_of_empty_text_is_one_empty_line() -> None:
    """Callers loop over the result and expect at least one row, so that a line
    with no description still occupies its row in the table.
    """
    assert wrap("", width=100.0, size=9.0) == [""]


def test_wrapping_loses_no_characters() -> None:
    value = "Levering van diensten volgens overeenkomst 2026-04 inclusief meerwerk"
    lines = wrap(value, width=80.0, size=9.0)

    assert "".join(lines).replace(" ", "") == value.replace(" ", "")


# --- pages -------------------------------------------------------------------


def test_multiple_pages_each_get_an_object_and_a_stream() -> None:
    pages = [_page_with(f"page {n}") for n in range(1, 4)]
    data = render_pdf(pages, title="x")

    assert data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") == 3
    assert b"/Count 3" in data
    # `<< /Length` rather than `stream\n`, because `endstream\n` contains the
    # latter and would double the count. Three Contents streams, plus one
    # more: the single ToUnicode CMap both fonts share (FR-TPL-016) is the
    # same minimal `<< /Length N >>` shape and matches too. The XMP Metadata
    # stream does NOT match - its dict carries other keys before /Length, so
    # the literal substring `<< /Length ` never occurs in it.
    assert data.count(b"<< /Length ") == 3 + 1


def test_every_page_is_a_kid_of_the_pages_node() -> None:
    pages = [_page_with(f"page {n}") for n in range(1, 4)]
    data = render_pdf(pages, title="x")

    kids = re.search(rb"/Kids \[(.*?)\]", data).group(1)
    assert len(re.findall(rb"\d+ 0 R", kids)) == 3


def test_rendering_no_pages_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one page"):
        render_pdf([], title="x")


def test_an_empty_text_run_is_skipped() -> None:
    """A blank optional field must not emit `() Tj`, which is legal but wastes
    bytes on every invoice with an unset supply date.
    """
    page = Page()
    page.at(x=10, top=10, value="")
    page.at(x=10, top=20, value="real")

    data = render_pdf([page], title="x")
    assert data.count(b" Tj") == 1


def test_bold_and_regular_both_reach_the_content_stream() -> None:
    page = Page()
    page.at(x=10, top=10, value="normal", font=Font.REGULAR)
    page.at(x=10, top=24, value="heading", font=Font.BOLD)

    data = render_pdf([page], title="x")
    assert b"/F1 " in data
    assert b"/F2 " in data


def _page_with(value: str) -> Page:
    page = Page()
    page.at(x=56, top=56, value=value)
    return page


# =============================================================================
# Embedded fonts (Type0/CIDFontType2) - the mechanism behind PDF/A-3's
# fonts_embedded, exercised against tests/invoicing/support/synthetic_font.py.
# =============================================================================


def test_an_embedded_font_produces_a_type0_and_fontfile2() -> None:
    embedded = _embedded({CODEPOINTS["A"]})
    page = Page()
    page.at(x=56, top=56, value="A", font="F3")
    data = render_pdf([page], title="x", embedded_fonts=(embedded,))

    assert b"/Subtype /Type0" in data
    assert b"/Encoding /Identity-H" in data
    assert b"/Subtype /CIDFontType2" in data
    assert b"/CIDToGIDMap /Identity" in data
    assert b"/FontFile2" in data


def test_an_embedded_font_run_is_shown_as_a_hex_cid_string() -> None:
    """Identity-H text is `<XXXX...>` hex, never `(...)` WinAnsi - the base-14
    literal-string path must not apply to a font resource that names an
    embedded font.
    """
    embedded = _embedded({CODEPOINTS["A"]})
    page = Page()
    page.at(x=56, top=56, value="A", font="F3")
    data = render_pdf([page], title="x", embedded_fonts=(embedded,))

    cid = embedded.codepoint_to_cid[CODEPOINTS["A"]]
    expected = b"<%04X> Tj" % cid
    assert expected in data


def test_base14_text_alongside_an_embedded_font_still_uses_winansi() -> None:
    """The additive promise: a document mixing an embedded heading font with
    base-14 body text must still draw the base-14 run as a literal string,
    unaffected by `embedded_fonts` being non-empty.
    """
    embedded = _embedded({CODEPOINTS["A"]})
    page = Page()
    page.at(x=56, top=56, value="A", font="F3")
    page.at(x=56, top=80, value="body text", font=Font.REGULAR)
    data = render_pdf([page], title="x", embedded_fonts=(embedded,))

    assert b"(body text) Tj" in data


def test_fonts_embedded_is_true_only_when_every_used_font_is_embedded() -> None:
    """The genuine computation `render_pdf` now does, replacing the old
    hardcoded `False` - checked here through the ONLY observable effect a
    caller has: an archival request that should now succeed (all text
    embedded) versus one that should still refuse (some text is not).
    """
    embedded = _embedded({CODEPOINTS["A"]})

    all_embedded = Page()
    all_embedded.at(x=56, top=56, value="A", font="F3")
    render_pdf(
        [all_embedded],
        title="x",
        conformance=Conformance.PDF_A_3B,
        icc_profile=FAKE_ICC,
        embedded_fonts=(embedded,),
    )  # must not raise

    mixed = Page()
    mixed.at(x=56, top=56, value="A", font="F3")
    mixed.at(x=56, top=80, value="body", font=Font.REGULAR)
    with pytest.raises(Exception, match="6.3.4"):
        render_pdf(
            [mixed],
            title="x",
            conformance=Conformance.PDF_A_3B,
            icc_profile=FAKE_ICC,
            embedded_fonts=(embedded,),
        )


def test_the_conformance_error_names_which_font_is_not_embedded() -> None:
    """D5: not just "a font is not embedded" but WHICH one, so a mixed
    embedded-heading/base-14-body document's error is actionable.
    """
    page = Page()
    page.at(x=56, top=56, value="plain body text")
    with pytest.raises(Exception, match="Helvetica"):
        render_pdf([page], title="x", conformance=Conformance.PDF_A_3B, icc_profile=FAKE_ICC)


def test_a_document_with_no_embedded_fonts_still_refuses_archival_conformance() -> None:
    """Unchanged behaviour for the common case (no deployment-configured
    font): the gate still refuses, honestly, exactly as before this change.
    """
    page = Page()
    page.at(x=56, top=56, value="plain body text")
    with pytest.raises(Exception, match="6.3.4"):
        render_pdf([page], title="x", conformance=Conformance.PDF_A_3B, icc_profile=FAKE_ICC)


def test_an_embedded_font_resource_name_cannot_shadow_a_base14_name() -> None:
    embedded = _embedded({CODEPOINTS["A"]}, resource_name=b"F1")
    page = Page()
    page.at(x=56, top=56, value="A", font="F1")
    with pytest.raises(ValueError, match="reserved"):
        render_pdf([page], title="x", embedded_fonts=(embedded,))


def test_two_embedded_fonts_cannot_share_a_resource_name() -> None:
    first = _embedded({CODEPOINTS["A"]}, resource_name=b"F3")
    second = _embedded({CODEPOINTS["A"]}, resource_name=b"F3")
    page = Page()
    page.at(x=56, top=56, value="A", font="F3")
    with pytest.raises(ValueError, match="F3"):
        render_pdf([page], title="x", embedded_fonts=(first, second))


def test_existing_base14_only_rendering_is_byte_identical_with_the_new_parameter() -> None:
    """The additive promise, proven at the byte level: calling `render_pdf`
    with no `embedded_fonts` (the default) produces EXACTLY the bytes it did
    before this parameter existed - the whole reason
    `test_multiple_pages_each_get_an_object_and_a_stream`'s exact object-count
    arithmetic above still passes unmodified.
    """
    page = _page_with("Factuur 2026-1")
    with_default = render_pdf([page], title="x")
    explicit_empty = render_pdf([_page_with("Factuur 2026-1")], title="x", embedded_fonts=())
    assert with_default == explicit_empty


# =============================================================================
# Colour - FR-TPL-004, new with this change
# =============================================================================


def test_color_from_hex_parses_rrggbb() -> None:
    color = Color.from_hex("#0f172a")
    assert color.red == pytest.approx(0x0F / 255.0)
    assert color.green == pytest.approx(0x17 / 255.0)
    assert color.blue == pytest.approx(0x2A / 255.0)


def test_a_colored_text_run_emits_an_rg_operator() -> None:
    page = Page()
    page.at(x=56, top=56, value="Accent", color=Color.from_hex("#ff0000"))
    data = render_pdf([page], title="x")

    assert b"1.0000 0.0000 0.0000 rg" in data


def test_a_colored_run_does_not_leak_into_the_next_one() -> None:
    """`rg` sets the FILL colour in the general graphics state, which
    persists across `BT`/`ET` blocks - `q`/`Q` bracketing is what stops a
    heading's accent colour bleeding into ordinary body text drawn after it.
    """
    page = Page()
    page.at(x=56, top=56, value="Accent", color=Color.from_hex("#ff0000"))
    page.at(x=56, top=80, value="Body text")
    data = render_pdf([page], title="x")

    body_start = data.index(b"(Body text)")
    close = data.rfind(b"Q\n", 0, body_start)
    # The colored run's own `Q` must close BEFORE the body run starts, so the
    # body run is not still inside that `q ... Q` bracket.
    assert close != -1 and close < body_start


def test_uncolored_text_is_byte_identical_to_before_color_existed() -> None:
    assert render_pdf([_page_with("plain")], title="x") == render_pdf(
        [_page_with("plain")], title="x"
    )
    # No `rg` operator at all when no run requests a colour.
    assert b" rg" not in render_pdf([_page_with("plain")], title="x")


def test_a_colored_rule_emits_an_rg_operator() -> None:
    page = Page()
    page.rule(x1=56, top=100, x2=400, color=Color.from_hex("#0f172a"))
    data = render_pdf([page], title="x")
    assert b" RG" in data


def test_an_uncolored_rule_is_unchanged_from_before_color_existed() -> None:
    page = Page()
    page.rule(x1=56, top=100, x2=400)
    data = render_pdf([page], title="x")
    assert b" RG" not in data
    assert b"0.50 w 56.00" in data


def test_a_filled_rectangle_is_an_artifact_with_no_structure_node() -> None:
    page = Page()
    page.fill_rect(x=0, top=0, width=595, height=100, color=Color.from_hex("#eeeeee"))
    page.at(x=56, top=56, value="content")
    data = render_pdf([page], title="x")

    assert b" re f" in data
    # Exactly one StructElem leaf for the one real text run - the rectangle
    # must not have produced a second one.
    assert data.count(b"/Type /StructElem") >= 1
    leaves = re.findall(rb"/Type /StructElem /S /P .*?/K (\d+)", data)
    assert leaves == [b"0"]


# =============================================================================
# Letter spacing (`Tc`) - FR-TPL-003, new with this change
# =============================================================================


def test_a_letter_spaced_text_run_emits_a_tc_operator() -> None:
    page = Page()
    page.at(x=56, top=56, value="Spaced", letter_spacing=0.5)
    data = render_pdf([page], title="x")

    assert b"0.50 Tc" in data


def test_no_letter_spacing_text_is_byte_identical_to_before_letter_spacing_existed() -> None:
    """The additive promise `Color` set, proven again for `Tc`: a `Text` that
    never asks for letter spacing (the `0.0` default, still true of every
    existing call site) produces exactly the same bytes as before this field
    existed - no `Tc` operator, no `q`/`Q` bracket that was not there before.
    """
    assert render_pdf([_page_with("plain")], title="x") == render_pdf(
        [_page_with("plain")], title="x"
    )
    assert b" Tc" not in render_pdf([_page_with("plain")], title="x")


def test_a_negative_letter_spacing_emits_a_negative_tc_value() -> None:
    """FR-TPL-003's `tight` step - a small negative `Tc`, not the leading `-`
    stripped or rejected."""
    page = Page()
    page.at(x=56, top=56, value="Tight", letter_spacing=-0.2)
    data = render_pdf([page], title="x")

    assert b"-0.20 Tc" in data


def test_a_letter_spaced_run_does_not_leak_into_the_next_one() -> None:
    """`Tc` sets character spacing in the GENERAL graphics state, which
    persists across `BT`/`ET` blocks exactly like `rg` does - `q`/`Q`
    bracketing is what stops a heading's letter spacing bleeding into
    ordinary body text drawn after it.
    """
    page = Page()
    page.at(x=56, top=56, value="Spaced", letter_spacing=0.5)
    page.at(x=56, top=80, value="Body text")
    data = render_pdf([page], title="x")

    body_start = data.index(b"(Body text)")
    close = data.rfind(b"Q\n", 0, body_start)
    assert close != -1 and close < body_start


def test_color_and_letter_spacing_combine_in_one_bracketed_run() -> None:
    """Both graphics-state changes on one run share a single `q`/`Q` bracket,
    not two nested ones - see `_content_stream`'s combined `state` list."""
    page = Page()
    page.at(x=56, top=56, value="Both", color=Color.from_hex("#ff0000"), letter_spacing=0.5)
    data = render_pdf([page], title="x")

    assert b"q 1.0000 0.0000 0.0000 rg 0.50 Tc\n" in data


def test_a_page_s_default_letter_spacing_applies_to_a_run_that_names_none_of_its_own() -> None:
    """`api.invoicing.rendering` sets `Page.default_letter_spacing` once per
    page rather than passing `letter_spacing` to every `at()` call - this is
    the mechanism that makes that work.
    """
    page = Page(default_letter_spacing=0.3)
    page.at(x=56, top=56, value="Inherits the page default")
    data = render_pdf([page], title="x")

    assert b"0.30 Tc" in data


def test_an_explicit_letter_spacing_overrides_the_page_default() -> None:
    page = Page(default_letter_spacing=0.3)
    page.at(x=56, top=56, value="Overridden", letter_spacing=0.0)
    data = render_pdf([page], title="x")

    assert b" Tc" not in data
