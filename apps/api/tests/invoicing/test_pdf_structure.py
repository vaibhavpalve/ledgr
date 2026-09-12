"""Tagged structure in api.invoicing.pdf - FR-TPL-016, and render_pdf's
conformance/icc_profile parameters that carry FR-TPL-015.

There is no PDF parser in this project's dependencies (the same constraint
`test_pdf.py` and `test_pdf_images.py` work under), so these are structural
tests over the bytes `render_pdf` produces: that a StructTreeRoot exists and
resolves to real StructElem objects, that every marked-content item in a
content stream is either tagged or explicitly an artifact, and that the
/ParentTree correctly maps each page's MCIDs back to the element that owns
them. A validator (veraPDF, PAC) would check exactly these things; this file
checks them by hand because none is available here.

The property worth the most attention in this file is the one named directly
in the brief: `test_the_preview_and_the_final_pdf_are_the_same_call` proves
FR-TPL-008 by asserting there is exactly ONE PDF-producing function in this
package - not that two code paths happen to agree today.
"""

from __future__ import annotations

import re

import pytest

from api.invoicing.pdf import (
    ImageError,
    Page,
    Placement,
    StructureTag,
    Text,
    load_image,
    render_pdf,
)
from api.invoicing.pdfa import Conformance, ConformanceNotMet
from tests.invoicing.image_fixtures import png

SRGB = b"fake-icc-profile-bytes-not-a-real-profile"


def _obj(data: bytes, number: int) -> bytes:
    """The raw body of object `number`, for inspecting one dictionary without
    a full parser - matches everything up to the next `endobj`.
    """
    match = re.search(rb"\n%d 0 obj\n(.*?)\nendobj\n" % number, data, re.S)
    assert match is not None, f"object {number} not found"
    return match.group(1)


def _catalog(data: bytes) -> bytes:
    return _obj(data, 1)


def _struct_tree_root(data: bytes) -> bytes:
    return _obj(data, 8)


def _all_struct_elems(data: bytes) -> dict[int, bytes]:
    """Every `/Type /StructElem` object, keyed by its object number.

    The pattern matches only up to `endobj` - NOT through its trailing
    newline. `finditer` resumes scanning immediately after the previous
    match, so a pattern that consumed that newline would leave the next
    object's own leading `\\n` already used up, and every other consecutive
    StructElem would go unmatched. Confirmed the hard way: an earlier version
    of this helper found the Document element and silently dropped every leaf
    that immediately followed it.
    """
    found: dict[int, bytes] = {}
    for match in re.finditer(rb"\n(\d+) 0 obj\n(<< /Type /StructElem .*?)\nendobj", data, re.S):
        found[int(match.group(1))] = match.group(2)
    return found


def _content_stream_of(data: bytes, index: int = -1) -> bytes:
    return re.findall(rb"stream\n(.*?)\nendstream", data, re.S)[index]


# =============================================================================
# FR-TPL-008: one engine
# =============================================================================


def test_the_preview_and_the_final_pdf_are_the_same_call() -> None:
    """FR-TPL-008: "what is previewed is what is sent. No separate preview
    renderer."

    This is not provable by comparing two outputs - a second renderer could
    happen to agree today and diverge tomorrow, and a test asserting bytes
    equal would only ever prove that. What actually satisfies the requirement
    is that there is nowhere ELSE in this package for a second renderer to
    live: exactly one function in `api.invoicing.pdf` turns a list of `Page`
    into PDF bytes, and it is the same `render_pdf` a preview endpoint and the
    FR-TPL-017 issue path both would have to call, because there is no other
    name to call instead.
    """
    import api.invoicing.pdf as pdf_module

    # `load_image` and `winansi_codepoints` are excluded by name: neither
    # turns a list of `Page` into PDF bytes - the first decodes a logo, the
    # second reports the code-point set `api.invoicing.rendering`'s font
    # loader subsets an embedded font to. Excluding them by name rather than
    # widening the shape this loop matches keeps the assertion meaningful: a
    # THIRD lowercase lower-level helper added later still has to justify
    # itself here rather than silently pass through an ever-looser filter.
    excluded = {"load_image", "winansi_codepoints"}
    producers = [
        name
        for name, value in vars(pdf_module).items()
        if callable(value)
        and getattr(value, "__module__", None) == pdf_module.__name__
        and name in pdf_module.__all__
        and name[0].isupper() is False
        and name not in excluded
    ]

    assert producers == ["render_pdf"], (
        f"expected render_pdf to be the ONLY public PDF-producing function in "
        f"api.invoicing.pdf; found {producers}. A second one is exactly the "
        f"'separate preview renderer' FR-TPL-008 rules out."
    )


def test_identical_input_produces_identical_bytes_every_time() -> None:
    """The narrower, mechanical half of FR-TPL-008: a preview showing settings
    as they change and a final send moments later must render the SAME
    settings to the SAME bytes, or "what is previewed is what is sent" is
    false even with one engine.
    """
    page = Page()
    page.at(x=56, top=56, value="Factuur 2026-1")

    first = render_pdf([page], title="x")
    second = render_pdf([page], title="x")
    assert first == second


# =============================================================================
# StructureTag / Text / Placement validation
# =============================================================================


def test_a_table_cell_without_a_row_is_refused() -> None:
    with pytest.raises(ValueError, match="row number"):
        Text(x=0, y=0, value="12,00", tag=StructureTag.TABLE_CELL, row=None)


def test_a_table_cell_with_a_row_is_accepted() -> None:
    cell = Text(x=0, y=0, value="12,00", tag=StructureTag.TABLE_CELL, row=0)
    assert cell.row == 0


def test_a_paragraph_needs_no_row() -> None:
    Text(x=0, y=0, value="Factuur", tag=StructureTag.PARAGRAPH)


@pytest.mark.parametrize("tag", [StructureTag.TABLE_HEADER, StructureTag.TABLE_CELL])
def test_both_cell_tags_require_a_row(tag: StructureTag) -> None:
    with pytest.raises(ValueError, match="row number"):
        Text(x=0, y=0, value="x", tag=tag, row=None)


def test_only_the_two_cell_tags_are_cells() -> None:
    cells = {tag for tag in StructureTag if tag.is_cell}
    assert cells == {StructureTag.TABLE_HEADER, StructureTag.TABLE_CELL}


def test_page_at_defaults_to_paragraph() -> None:
    page = Page()
    page.at(x=0, top=0, value="x")
    assert page.texts[0].tag is StructureTag.PARAGRAPH


def test_page_at_passes_through_tag_and_row() -> None:
    page = Page()
    page.at(x=0, top=0, value="12,00", tag=StructureTag.TABLE_CELL, row=2)
    assert page.texts[0].tag is StructureTag.TABLE_CELL
    assert page.texts[0].row == 2


def test_page_at_passes_through_is_artifact() -> None:
    page = Page()
    page.at(x=0, top=0, value="Pagina 1 van 3", is_artifact=True)
    assert page.texts[0].is_artifact


def test_an_artifact_run_needs_no_row_even_with_a_cell_tag() -> None:
    """`is_artifact` short-circuits `Text.__post_init__` before the row check
    runs - tag/row are meaningless once a run is an artifact, so nothing
    about them should be able to raise.
    """
    Text(x=0, y=0, value="x", tag=StructureTag.TABLE_CELL, row=None, is_artifact=True)


def test_an_image_with_no_alt_text_is_refused() -> None:
    with pytest.raises(ImageError, match="alternative text"):
        Placement(image=load_image(png()), x=0, y=0, width=10, height=10, alt="")


def test_place_image_requires_alt_with_no_default() -> None:
    import inspect

    signature = inspect.signature(Page.place_image)
    assert signature.parameters["alt"].default is inspect.Parameter.empty


# =============================================================================
# The Catalog: MarkInfo, Lang, Metadata, StructTreeRoot
# =============================================================================


def _rendered(**kwargs: object) -> bytes:
    page = Page()
    page.at(x=56, top=56, value="Factuur 2026-1", tag=StructureTag.HEADING)
    return render_pdf([page], title="Factuur", **kwargs)  # type: ignore[arg-type]


def test_the_catalog_declares_the_document_marked() -> None:
    """`/MarkInfo << /Marked true >>` is what tells a reader "this file has a
    structure tree, look for it" rather than leaving it to guess from content.
    """
    assert b"/MarkInfo << /Marked true >>" in _catalog(_rendered())


def test_the_catalog_declares_a_language() -> None:
    """PDF/A 6.9 and, independently, a screen reader choosing pronunciation
    rules - both need this regardless of archival conformance.
    """
    assert b"/Lang (nl)" in _catalog(_rendered())


def test_the_language_parameter_is_honoured() -> None:
    assert b"/Lang (en)" in _catalog(_rendered(language="en"))


def test_the_catalog_points_at_the_metadata_stream() -> None:
    assert b"/Metadata 6 0 R" in _catalog(_rendered())


def test_the_metadata_object_is_the_xmp_packet() -> None:
    data = _rendered()
    metadata = _obj(data, 6)
    assert b"/Type /Metadata /Subtype /XML" in metadata
    assert b"<x:xmpmeta" in metadata


def test_the_catalog_points_at_the_struct_tree_root() -> None:
    assert b"/StructTreeRoot 8 0 R" in _catalog(_rendered())


def test_no_output_intents_without_an_icc_profile() -> None:
    assert b"/OutputIntents" not in _catalog(_rendered())


def test_output_intents_appear_when_an_icc_profile_is_given() -> None:
    """Independent of whether `conformance` itself is achievable - see the
    module docstring. This is what lets the OutputIntent mechanism be proven
    correct today even though a full archival render cannot yet succeed.
    """
    data = _rendered(icc_profile=SRGB)
    assert b"/OutputIntents [" in _catalog(data)


# =============================================================================
# The structure tree
# =============================================================================


def test_the_struct_tree_root_has_the_right_type_and_one_child() -> None:
    root = _struct_tree_root(_rendered())
    assert b"/Type /StructTreeRoot" in root
    assert re.search(rb"/K \[9 0 R\]", root)


def test_the_document_element_is_object_nine() -> None:
    elems = _all_struct_elems(_rendered())
    assert 9 in elems
    assert b"/S /Document" in elems[9]


def test_every_struct_elem_has_a_parent() -> None:
    """ISO 32000 7.9.4 requires /P on every structure element. Missing it is
    invisible to a casual look at the file and is exactly the kind of gap a
    validator exists to catch - checked here since none is available.
    """
    for number, body in _all_struct_elems(_rendered()).items():
        assert b"/P " in body, f"struct elem {number} has no parent"


def test_the_documents_parent_is_the_struct_tree_root() -> None:
    elems = _all_struct_elems(_rendered())
    assert b"/P 8 0 R" in elems[9]


def test_a_heading_becomes_its_own_struct_elem_under_document() -> None:
    data = _rendered()  # the fixture page has one H1
    elems = _all_struct_elems(data)
    document = elems[9]

    heading_numbers = [n for n, body in elems.items() if b"/S /H1" in body]
    assert len(heading_numbers) == 1
    assert (b"%d 0 R" % heading_numbers[0]) in document


def test_a_leaf_struct_elem_carries_its_page_and_a_bare_mcid() -> None:
    elems = _all_struct_elems(_rendered())
    heading = next(body for body in elems.values() if b"/S /H1" in body)

    assert b"/Pg " in heading
    # A bare integer /K (not an array, not a dict) - ISO 32000 7.9.4's
    # shorthand for a single marked-content reference.
    assert re.search(rb"/K 0 >>", heading)


def test_a_figure_carries_its_alt_text() -> None:
    page = Page()
    page.place_image(
        image=load_image(png()), x=56, top=40, width=60, height=60, alt="Bakker Consultancy"
    )
    data = render_pdf([page], title="x")

    elems = _all_struct_elems(data)
    figure = next(body for body in elems.values() if b"/S /Figure" in body)
    assert b"/Alt (Bakker Consultancy)" in figure


def test_a_figure_with_a_paren_in_its_alt_text_is_escaped() -> None:
    """ "Bakker (Holding) B.V." is an ordinary company name - the same
    ordinary-data argument `test_pdf.py` makes for text runs, applying here to
    /Alt.
    """
    page = Page()
    page.place_image(
        image=load_image(png()), x=56, top=40, width=60, height=60, alt="Bakker (Holding) B.V."
    )
    data = render_pdf([page], title="x")

    figure = next(body for body in _all_struct_elems(data).values() if b"/S /Figure" in body)
    assert b"Bakker \\(Holding\\) B.V." in figure


# --- table grouping ----------------------------------------------------------


def _table_page() -> Page:
    page = Page()
    page.at(x=56, top=100, value="Omschrijving", tag=StructureTag.TABLE_HEADER, row=0)
    page.at(x=200, top=100, value="Bedrag", tag=StructureTag.TABLE_HEADER, row=0)
    page.at(x=56, top=112, value="Advies", tag=StructureTag.TABLE_CELL, row=1)
    page.at(x=200, top=112, value="1.000,00", tag=StructureTag.TABLE_CELL, row=1)
    page.at(x=56, top=124, value="Onderzoek", tag=StructureTag.TABLE_CELL, row=2)
    page.at(x=200, top=124, value="500,00", tag=StructureTag.TABLE_CELL, row=2)
    return page


def test_cells_on_one_row_share_one_tr() -> None:
    data = render_pdf([_table_page()], title="x")
    elems = _all_struct_elems(data)

    rows = {n: body for n, body in elems.items() if b"/S /TR" in body}
    assert len(rows) == 3  # header row + two data rows

    header_row = next(body for body in rows.values() if b"/K [" in body)
    # Both header cells (two /K refs) belong to the SAME TR.
    refs = re.findall(rb"(\d+) 0 R", re.search(rb"/K \[(.*?)\]", header_row).group(1))
    assert len(refs) == 2


def test_rows_are_grouped_under_one_table() -> None:
    data = render_pdf([_table_page()], title="x")
    elems = _all_struct_elems(data)

    tables = [n for n, body in elems.items() if b"/S /Table" in body]
    assert len(tables) == 1

    table_body = elems[tables[0]]
    row_refs = re.findall(rb"(\d+) 0 R", re.search(rb"/K \[(.*?)\]", table_body).group(1))
    assert len(row_refs) == 3


def test_a_paragraph_between_two_tables_produces_two_tables() -> None:
    """A line-item table, a subtotal paragraph, then a second small table -
    they must not merge into one, or a screen reader announces the subtotal
    as though it were part of the line items.
    """
    page = _table_page()
    page.at(x=56, top=150, value="Subtotaal", tag=StructureTag.PARAGRAPH)
    page.at(x=56, top=170, value="Korting", tag=StructureTag.TABLE_HEADER, row=0)
    page.at(x=56, top=182, value="5%", tag=StructureTag.TABLE_CELL, row=1)

    data = render_pdf([page], title="x")
    elems = _all_struct_elems(data)

    tables = [n for n, body in elems.items() if b"/S /Table" in body]
    assert len(tables) == 2


def test_an_image_does_not_close_a_table_spanning_pages() -> None:
    """FR-TPL-010's repeating headers: page two of a multi-page invoice draws
    the letterhead again before continuing the line-item table. The image
    must not split what should be one continuous Table into two.
    """
    first = _table_page()
    second = Page()
    second.place_image(
        image=load_image(png()), x=56, top=20, width=40, height=40, alt="Bakker Consultancy"
    )
    second.at(x=56, top=100, value="Vervoer", tag=StructureTag.TABLE_CELL, row=3)
    second.at(x=200, top=100, value="80,00", tag=StructureTag.TABLE_CELL, row=3)

    data = render_pdf([first, second], title="x")
    elems = _all_struct_elems(data)

    tables = [n for n, body in elems.items() if b"/S /Table" in body]
    assert len(tables) == 1

    table_body = elems[tables[0]]
    row_refs = re.findall(rb"(\d+) 0 R", re.search(rb"/K \[(.*?)\]", table_body).group(1))
    # 3 rows on page one (header + 2 data) + 1 more on page two = 4.
    assert len(row_refs) == 4


def test_an_artifact_text_run_does_not_close_a_table_spanning_pages() -> None:
    """The bug this whole property was written to catch: a footer drawn on
    the OLD page right after its last line item and right before the NEW
    page's repeated header. An ordinary PARAGRAPH there would end the table
    at the page break - `is_artifact=True` is what keeps it open, exactly
    like an image does for the same reason.
    """
    first = _table_page()
    first.at(x=56, top=200, value="KvK 12345678", is_artifact=True)

    second = Page()
    second.at(x=56, top=100, value="Vervoer", tag=StructureTag.TABLE_CELL, row=3)
    second.at(x=200, top=100, value="80,00", tag=StructureTag.TABLE_CELL, row=3)

    data = render_pdf([first, second], title="x")
    elems = _all_struct_elems(data)

    tables = [n for n, body in elems.items() if b"/S /Table" in body]
    assert len(tables) == 1


def test_an_artifact_text_run_is_absent_from_the_structure_tree_entirely() -> None:
    """Unlike a tagged run, which always becomes a leaf `_StructNode`
    somewhere, an artifact contributes nothing - no `/P`, no `/TD`, nothing
    a reader would ever navigate to.
    """
    page = Page()
    page.at(x=56, top=56, value="Pagina 1 van 1", is_artifact=True)

    data = render_pdf([page], title="x")
    elems = _all_struct_elems(data)

    # Only the Document root - the artifact produced no leaf.
    assert len(elems) == 1
    assert b"/S /Document" in elems[9]


def test_an_empty_text_run_consumes_no_mcid() -> None:
    """`_content_stream` makes no mark for an empty run, so the structure
    tree must not reserve an MCID for it either - a gap here would
    desynchronise every mcid after it from what is actually drawn.
    """
    page = Page()
    page.at(x=56, top=56, value="", tag=StructureTag.PARAGRAPH)
    page.at(x=56, top=68, value="Factuur", tag=StructureTag.PARAGRAPH)

    data = render_pdf([page], title="x")
    heading = next(body for body in _all_struct_elems(data).values() if b"/S /P" in body)
    assert re.search(rb"/K 0 >>", heading), "the surviving run should own MCID 0, not 1"


# =============================================================================
# ParentTree and /StructParents
# =============================================================================


def test_every_page_carries_a_struct_parents_index() -> None:
    pages = [Page(), Page(), Page()]
    for index, page in enumerate(pages):
        page.at(x=56, top=56, value=f"page {index}")
    data = render_pdf(pages, title="x")

    page_dicts = re.findall(rb"/Type /Page /Parent.*?/Contents \d+ 0 R[^>]*>>", data, re.S)
    assert len(page_dicts) == 3
    for index, page_dict in enumerate(page_dicts):
        assert b"/StructParents %d" % index in page_dict


def test_the_parent_tree_resolves_a_pages_mcid_to_its_struct_elem() -> None:
    """The mechanism a reader actually walks: /StructParents on the page finds
    the right entry in /ParentTree's /Nums, and that entry's array, indexed by
    MCID, gives the owning structure element. Wired correctly end to end here
    rather than just "the pieces individually exist".
    """
    data = _rendered()
    root = _struct_tree_root(data)

    nums = re.search(rb"/Nums \[(.*)\]", root, re.S).group(1)
    # Page 0's entry: "0 [<ref>]".
    page_zero = re.search(rb"0 \[(\d+) 0 R\]", nums)
    assert page_zero is not None

    referenced_number = int(page_zero.group(1))
    elems = _all_struct_elems(data)
    assert referenced_number in elems
    assert b"/S /H1" in elems[referenced_number]


def test_parent_tree_next_key_is_the_page_count() -> None:
    pages = [Page(), Page()]
    for page in pages:
        page.at(x=56, top=56, value="x")
    root = _struct_tree_root(render_pdf(pages, title="x"))

    assert b"/ParentTreeNextKey 2" in root


def test_a_page_with_nothing_tagged_gets_an_empty_parent_tree_entry() -> None:
    """A page carrying only a rule (an artifact) has no leaf structure element
    at all - the Nums entry for it must be a well-formed empty array, not
    absent or malformed.
    """
    page = Page()
    page.rule(x1=56, top=100, x2=400)
    data = render_pdf([page], title="x")

    root = _struct_tree_root(data)
    assert re.search(rb"/Nums \[0 \[\]\]", root)


# =============================================================================
# The content stream: BDC/EMC tagging and /Artifact
# =============================================================================


def test_a_text_run_is_wrapped_in_bdc_emc_naming_its_tag_and_mcid() -> None:
    page = Page()
    page.at(x=56, top=56, value="Factuur", tag=StructureTag.HEADING)
    stream = _content_stream_of(render_pdf([page], title="x"))

    assert re.search(rb"/H1 << /MCID 0 >> BDC\nBT .*? Tj ET\nEMC", stream, re.S)


def test_a_rule_is_marked_as_an_artifact_not_tagged_content() -> None:
    """A rule carries no meaning a reader should announce. Tagging it as real
    content would have a screen reader say "line" between every section;
    leaving it neither tagged nor marked is the "unmarked content" gap a
    structure validator flags. /Artifact is the third option that is correct.
    """
    page = Page()
    page.rule(x1=56, top=100, x2=400)
    stream = _content_stream_of(render_pdf([page], title="x"))

    assert re.search(rb"/Artifact BDC\n.*? S\nEMC", stream, re.S)
    assert b"/MCID" not in stream


def test_an_artifact_text_run_is_marked_artifact_not_tagged() -> None:
    """A running footer or page number, drawn as text rather than a rule -
    the same `/Artifact` treatment, and no `/MCID` at all.
    """
    page = Page()
    page.at(x=56, top=100, value="Pagina 1 van 1", is_artifact=True)
    stream = _content_stream_of(render_pdf([page], title="x"))

    assert re.search(rb"/Artifact BDC\nBT .*? Tj ET\nEMC", stream, re.S)
    assert b"/MCID" not in stream


def test_an_image_is_tagged_figure_with_its_mcid() -> None:
    page = Page()
    page.place_image(image=load_image(png()), x=56, top=40, width=60, height=60, alt="Logo")
    stream = _content_stream_of(render_pdf([page], title="x"))

    assert re.search(rb"/Figure << /MCID 0 >> BDC\n.*? Do Q\nEMC", stream, re.S)


def test_images_and_text_get_distinct_mcids_on_one_page() -> None:
    page = Page()
    page.place_image(image=load_image(png()), x=56, top=40, width=60, height=60, alt="Logo")
    page.at(x=56, top=120, value="Factuur", tag=StructureTag.HEADING)
    stream = _content_stream_of(render_pdf([page], title="x"))

    assert b"/Figure << /MCID 0 >> BDC" in stream
    assert b"/H1 << /MCID 1 >> BDC" in stream


def test_mcids_restart_at_zero_on_each_page() -> None:
    """/StructParents is per page, so MCID 0 on page two must not collide
    with MCID 0 on page one - they are looked up through different Nums
    entries entirely.
    """
    first = Page()
    first.at(x=56, top=56, value="page one", tag=StructureTag.HEADING)
    second = Page()
    second.at(x=56, top=56, value="page two", tag=StructureTag.HEADING)

    data = render_pdf([first, second], title="x")
    streams = re.findall(rb"stream\n(.*?)\nendstream", data, re.S)

    # The two Contents streams are the last two streams (Metadata and
    # ToUnicode precede every page).
    assert b"/H1 << /MCID 0 >> BDC" in streams[-2]
    assert b"/H1 << /MCID 0 >> BDC" in streams[-1]


def test_every_bdc_is_closed_by_an_emc() -> None:
    """An unbalanced BDC/EMC pair corrupts the marked-content nesting for
    everything drawn after it - the content-stream equivalent of an unescaped
    paren.
    """
    page = _table_page()
    page.place_image(image=load_image(png()), x=56, top=10, width=20, height=20, alt="Logo")
    page.rule(x1=56, top=200, x2=400)
    stream = _content_stream_of(render_pdf([page], title="x"))

    assert stream.count(b" BDC\n") == stream.count(b"EMC\n")


# =============================================================================
# ToUnicode wiring
# =============================================================================


def test_both_fonts_reference_the_same_tounicode_object() -> None:
    data = _rendered()
    assert data.count(b"/ToUnicode 7 0 R") == 2


def test_the_tounicode_object_is_a_cmap_stream() -> None:
    cmap = _obj(_rendered(), 7)
    assert b"beginbfchar" in cmap


def test_the_euro_sign_round_trips_through_tounicode() -> None:
    """The euro sign is WinAnsi byte 0x80 (`_encode`); the CMap must map that
    same byte back to U+20AC or copying the text out of a reader produces a
    control character instead of a euro sign.
    """
    cmap = _obj(_rendered(), 7)
    assert b"<80> <20AC>" in cmap


# =============================================================================
# render_pdf's conformance gate
# =============================================================================


def test_conformance_none_is_the_default_and_succeeds() -> None:
    page = Page()
    page.at(x=56, top=56, value="x")
    data = render_pdf([page], title="x")
    assert data.startswith(b"%PDF-1.7")


@pytest.mark.parametrize("target", [Conformance.PDF_A_3A, Conformance.PDF_A_3B])
def test_requesting_archival_conformance_raises_today(target: Conformance) -> None:
    """The central, load-bearing behaviour: `render_pdf` never emits bytes
    claiming a conformance it cannot support. See api.invoicing.pdfa for why
    that is the correct posture rather than a limitation to work around here.
    """
    page = Page()
    page.at(x=56, top=56, value="x")

    with pytest.raises(ConformanceNotMet) as caught:
        render_pdf([page], title="x", conformance=target, icc_profile=SRGB)

    assert caught.value.target is target
    assert any(gap.clause == "6.3.4" for gap in caught.value.gaps)


def test_the_conformance_gate_runs_before_anything_is_built(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A cheap, early refusal - not a render that is thrown away and then
    discarded. Proven by making `_build_structure` itself raise if it is ever
    called, then confirming a `ConformanceNotMet` surfaces instead of that -
    which is only possible if the gate ran BEFORE `_build_structure` did.
    """
    import api.invoicing.pdf as pdf_module

    def _must_not_run(pages: object) -> None:
        raise AssertionError("_build_structure ran despite the conformance gate")

    monkeypatch.setattr(pdf_module, "_build_structure", _must_not_run)

    page = Page()
    page.at(x=56, top=56, value="x")

    with pytest.raises(ConformanceNotMet):
        render_pdf([page], title="x", conformance=Conformance.PDF_A_3A)


def test_no_icc_profile_and_an_archival_target_names_both_gaps() -> None:
    page = Page()
    page.at(x=56, top=56, value="x")

    with pytest.raises(ConformanceNotMet) as caught:
        render_pdf([page], title="x", conformance=Conformance.PDF_A_3B, icc_profile=None)

    clauses = {gap.clause for gap in caught.value.gaps}
    assert {"6.3.4", "6.2.2"} <= clauses


# =============================================================================
# icc_profile embedding, independent of conformance
# =============================================================================


def test_an_icc_profile_is_embedded_as_a_flate_stream() -> None:
    page = Page()
    page.at(x=56, top=56, value="x")
    data = render_pdf([page], title="x", icc_profile=SRGB)

    output_intent_number = int(re.search(rb"/OutputIntents \[(\d+) 0 R\]", _catalog(data)).group(1))
    output_intent_body = _obj(data, output_intent_number)
    icc_number = int(re.search(rb"/DestOutputProfile (\d+) 0 R", output_intent_body).group(1))

    icc_stream = _obj(data, icc_number)
    assert b"/N 3 /Alternate /DeviceRGB /Filter /FlateDecode" in icc_stream

    import zlib

    compressed = re.search(rb"stream\n(.*)\nendstream", icc_stream, re.S).group(1)
    assert zlib.decompress(compressed) == SRGB


def test_no_icc_profile_means_no_outputintent_objects_at_all() -> None:
    data = _rendered()
    assert b"/Type /OutputIntent" not in data


# =============================================================================
# The file identifier
# =============================================================================


def test_the_trailer_carries_a_file_id() -> None:
    data = _rendered()
    match = re.search(rb"/ID \[<([0-9a-f]{32})> <([0-9a-f]{32})>\]", data)
    assert match is not None
    assert match.group(1) == match.group(2)


def test_the_file_id_is_a_function_of_the_document() -> None:
    """Deterministic (same invoice, same id - FR-TPL-017's content-hash
    argument) and distinguishing (a different invoice, a different id - or
    two different documents would be indistinguishable by their identifier,
    which is the one thing /ID exists to prevent).
    """
    page_a = Page()
    page_a.at(x=56, top=56, value="Factuur 2026-1")
    page_b = Page()
    page_b.at(x=56, top=56, value="Factuur 2026-2")

    first = render_pdf([page_a], title="x")
    again = render_pdf([page_a], title="x")
    different = render_pdf([page_b], title="x")

    def file_id(data: bytes) -> bytes:
        return re.search(rb"/ID \[<([0-9a-f]{32})>", data).group(1)

    assert file_id(first) == file_id(again)
    assert file_id(first) != file_id(different)


# =============================================================================
# xref integrity with the larger object graph
# =============================================================================


def test_every_xref_offset_points_at_its_object_with_full_tagging() -> None:
    """The same check `test_pdf.py` runs for the plain case, repeated with
    structure, metadata, ToUnicode and an ICC profile all present - the
    numbering arithmetic has the most new surface here, so this is where an
    off-by-one would show up.
    """
    data = _table_page()
    page = Page()
    page.at(x=56, top=56, value="Factuur", tag=StructureTag.HEADING)
    page.place_image(image=load_image(png()), x=56, top=80, width=40, height=40, alt="Logo")
    rendered = render_pdf([page, data], title="x", icc_profile=SRGB)

    start = int(re.search(rb"startxref\n(\d+)", rendered).group(1))
    offsets = [int(m) for m in re.findall(rb"^(\d{10}) 00000 n", rendered[start:], re.M)]

    assert offsets
    for number, offset in enumerate(offsets, start=1):
        assert rendered[offset:].startswith(b"%d 0 obj" % number), (
            f"xref entry {number} points at offset {offset}, which is not that object"
        )


def test_the_object_count_matches_the_trailer_with_full_tagging() -> None:
    page = Page()
    page.at(x=56, top=56, value="Factuur", tag=StructureTag.HEADING)
    page.place_image(image=load_image(png()), x=56, top=80, width=40, height=40, alt="Logo")
    data = render_pdf([page], title="x", icc_profile=SRGB)

    declared = int(re.search(rb"/Size (\d+)", data).group(1))
    actual = len(re.findall(rb"^\d+ 0 obj", data, re.M))
    assert declared == actual + 1  # /Size counts the free object 0 too


def test_a_page_names_only_the_images_it_draws_alongside_structure() -> None:
    """Guards against the structure-tree work having disturbed the
    per-page /XObject scoping `test_pdf_images.py` already established.
    """
    with_logo = Page()
    with_logo.place_image(image=load_image(png()), x=56, top=40, width=40, height=40, alt="Logo")
    without = Page()
    without.at(x=56, top=56, value="page two")

    data = render_pdf([with_logo, without], title="x")
    page_dicts = re.findall(rb"/Type /Page /Parent.*?/Contents", data, re.S)

    assert len(page_dicts) == 2
    assert b"/XObject" in page_dicts[0]
    assert b"/XObject" not in page_dicts[1]
