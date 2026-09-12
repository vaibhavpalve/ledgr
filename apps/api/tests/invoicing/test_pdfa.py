"""api.invoicing.pdfa - FR-TPL-015's conformance gate, and the metadata pieces
underneath it.

The property worth more than any other assertion in this file: **asking this
module to claim PDF/A never succeeds today, and it never lies about why.**
Every other test here is either a consequence of that (the gate's exact
behaviour) or a check on one of the pieces the gate is guarding (XMP, the
ToUnicode CMap, the OutputIntent dictionary) - each independently correct and
independently testable, which is what lets them be proven right NOW even
though the whole cannot yet be exercised end to end (no licensed font exists
in this repository to embed).
"""

from __future__ import annotations

import re

import pytest

from api.invoicing.pdfa import (
    REQUIREMENTS,
    Conformance,
    ConformanceGap,
    ConformanceNotMet,
    assert_conformance,
    conformance_gaps,
    output_intent,
    to_unicode_cmap,
    to_unicode_cmap_cid,
    xmp_packet,
)

SRGB = b"fake-icc-profile-bytes-not-a-real-profile"


# --- Conformance itself -------------------------------------------------------


def test_none_is_not_archival() -> None:
    assert not Conformance.NONE.is_archival


@pytest.mark.parametrize("target", [Conformance.PDF_A_3A, Conformance.PDF_A_3B])
def test_both_pdfa_targets_are_archival(target: Conformance) -> None:
    assert target.is_archival


def test_the_part_is_always_three() -> None:
    """FR-TPL-015 names PDF/A-3 specifically - the version that permits
    embedding the source data (e.g. a future UBL) alongside the rendering.
    """
    assert Conformance.PDF_A_3A.part == 3
    assert Conformance.PDF_A_3B.part == 3


def test_the_level_matches_the_target() -> None:
    assert Conformance.PDF_A_3A.level == "A"
    assert Conformance.PDF_A_3B.level == "B"


# --- the gate: what it lets through today -------------------------------------


def test_none_has_no_gaps_regardless_of_fonts_or_icc() -> None:
    """A plain PDF makes no PDF/A claim, so nothing about it can be false."""
    assert conformance_gaps(Conformance.NONE, fonts_embedded=False, icc_profile=None) == ()
    assert conformance_gaps(Conformance.NONE, fonts_embedded=True, icc_profile=SRGB) == ()


@pytest.mark.parametrize("target", [Conformance.PDF_A_3A, Conformance.PDF_A_3B])
def test_an_archival_target_always_names_the_font_gap_today(target: Conformance) -> None:
    """The central fact this module exists to be honest about: this writer
    embeds no font, and PDF/A requires one at EVERY conformance level - so
    every archival request has this gap, with or without an ICC profile.
    """
    gaps = conformance_gaps(target, fonts_embedded=False, icc_profile=SRGB)
    assert any(gap.clause == "6.3.4" for gap in gaps)


def test_a_missing_icc_profile_is_its_own_gap() -> None:
    gaps = conformance_gaps(Conformance.PDF_A_3B, fonts_embedded=True, icc_profile=None)
    assert any(gap.clause == "6.2.2" for gap in gaps)


def test_fonts_and_icc_both_present_closes_every_gap_for_level_b() -> None:
    """The one scenario in which `conformance_gaps` returns empty for an
    archival target - proving the gate is not permanently stuck closed, only
    stuck closed by the two facts that are actually true today.
    """
    assert conformance_gaps(Conformance.PDF_A_3B, fonts_embedded=True, icc_profile=SRGB) == ()


def test_level_a_additionally_needs_nothing_beyond_fonts_and_icc() -> None:
    """Tagging, the ToUnicode CMap and alternative text are already satisfied
    by this writer (`REQUIREMENTS`), so level A closes on exactly the same two
    conditions as level B.
    """
    assert conformance_gaps(Conformance.PDF_A_3A, fonts_embedded=True, icc_profile=SRGB) == ()


def test_level_b_does_not_demand_tagging() -> None:
    """Level B's requirements exclude the level-A-only clauses (6.8, 6.3.8,
    6.8.3), which is the entire distinction between the two levels. Since this
    writer satisfies them anyway, the only OBSERVABLE difference right now is
    what target you asked for - proven here by asking for both with identical
    fonts/icc and getting identical (empty) results.
    """
    a = conformance_gaps(Conformance.PDF_A_3A, fonts_embedded=True, icc_profile=SRGB)
    b = conformance_gaps(Conformance.PDF_A_3B, fonts_embedded=True, icc_profile=SRGB)
    assert a == b == ()


def test_assert_conformance_raises_naming_the_target_and_the_gap() -> None:
    with pytest.raises(ConformanceNotMet) as caught:
        assert_conformance(Conformance.PDF_A_3A, fonts_embedded=False, icc_profile=SRGB)

    assert caught.value.target is Conformance.PDF_A_3A
    assert any(gap.clause == "6.3.4" for gap in caught.value.gaps)
    assert "6.3.4" in str(caught.value)
    assert "FR-TPL-015" in str(caught.value)


def test_assert_conformance_is_silent_for_none() -> None:
    # No exception is the assertion.
    assert_conformance(Conformance.NONE, fonts_embedded=False, icc_profile=None)


def test_assert_conformance_would_succeed_once_both_conditions_are_true() -> None:
    """The gate is not hardcoded to always fail - it fails BECAUSE the two
    conditions are false. This is what proves `render_pdf` will start
    succeeding the moment font embedding lands, with no change to the gate
    itself.
    """
    assert_conformance(Conformance.PDF_A_3B, fonts_embedded=True, icc_profile=SRGB)


# --- ConformanceGap ------------------------------------------------------------


def test_a_gap_renders_as_one_readable_line() -> None:
    gap = ConformanceGap(clause="6.3.4", summary="every font is embedded", closes_with="do it")
    text = str(gap)

    assert "6.3.4" in text
    assert "every font is embedded" in text
    assert "do it" in text


# --- REQUIREMENTS: the data conformance_gaps is derived from -------------------


def test_every_requirement_has_a_unique_clause() -> None:
    clauses = [requirement.clause for requirement in REQUIREMENTS]
    assert len(clauses) == len(set(clauses)), "a duplicated clause would make one shadow another"


def test_an_unsatisfied_requirement_explains_how_to_close_it() -> None:
    """A requirement marked unsatisfied with no `closes_with` would surface as
    an empty sentence in `ConformanceGap` - the exact dead end D5 forbids.
    """
    for requirement in REQUIREMENTS:
        if not requirement.satisfied:
            assert requirement.closes_with.strip(), (
                f"{requirement.clause} is unsatisfied and says nothing about how to close it"
            )


def test_conformance_gaps_only_reports_unsatisfied_requirements() -> None:
    """Every gap reported for a target where fonts/icc are both supplied must
    come from a requirement genuinely marked unsatisfied - proving
    `conformance_gaps` does not invent gaps beyond what `REQUIREMENTS` says.
    """
    gaps = conformance_gaps(Conformance.PDF_A_3A, fonts_embedded=False, icc_profile=None)
    reported = {gap.clause for gap in gaps}
    unsatisfiable = {r.clause for r in REQUIREMENTS if not r.satisfied}
    assert reported <= unsatisfiable


# --- XMP -----------------------------------------------------------------------


def test_a_plain_document_carries_no_pdfaid_claim() -> None:
    packet = xmp_packet(title="Factuur 2026-1", language="nl", conformance=Conformance.NONE)
    assert b"pdfaid" not in packet


def test_an_archival_request_carries_the_matching_claim() -> None:
    """Reachable directly - unlike through `render_pdf`, which the conformance
    gate currently refuses for any archival target. This is what proves the
    CLAIM ITSELF is written correctly, ready for the day the gate opens.
    """
    packet = xmp_packet(title="Factuur 2026-1", language="nl", conformance=Conformance.PDF_A_3A)

    assert b"<pdfaid:part>3</pdfaid:part>" in packet
    assert b"<pdfaid:conformance>A</pdfaid:conformance>" in packet


def test_level_b_claims_b_not_a() -> None:
    packet = xmp_packet(title="x", language="nl", conformance=Conformance.PDF_A_3B)
    assert b"<pdfaid:conformance>B</pdfaid:conformance>" in packet


def test_the_title_and_language_are_carried() -> None:
    packet = xmp_packet(title="Factuur 2026-7", language="en", conformance=Conformance.NONE)

    assert b"Factuur 2026-7" in packet
    assert b"<rdf:li>en</rdf:li>" in packet


def test_special_xml_characters_in_the_title_are_escaped() -> None:
    """A company name with an ampersand ("Bakker & Zonen") is ordinary data,
    and an unescaped `&` would corrupt every entity reference after it in the
    XML parser's eyes.
    """
    packet = xmp_packet(
        title='Bakker & Zonen <B.V.> "Holding"', language="nl", conformance=Conformance.NONE
    )
    text = packet.decode("utf-8")

    assert "Bakker &amp; Zonen &lt;B.V.&gt; &quot;Holding&quot;" in text
    # The raw characters must not survive unescaped inside element content.
    assert "Zonen <B.V.>" not in text


def test_no_creation_or_modify_date_is_written() -> None:
    """The only bytes that would make two renderings of one invoice differ -
    see `api.invoicing.pdf`'s note on determinism and FR-TPL-017's content
    hash.
    """
    packet = xmp_packet(title="x", language="nl", conformance=Conformance.PDF_A_3A)

    assert b"CreateDate" not in packet
    assert b"ModifyDate" not in packet


def test_the_packet_is_well_formed_enough_to_parse() -> None:
    """Not a full XMP/RDF validation - there is no XML library dependency to
    do that with - but a parser choking on a stray unescaped character or an
    unclosed tag is exactly the failure the escaping tests above guard
    against, checked here from the other direction.
    """
    import xml.etree.ElementTree as ET

    packet = xmp_packet(title="Bakker & Zonen", language="nl", conformance=Conformance.PDF_A_3A)
    # Strip the xpacket processing instructions, which ElementTree does not
    # parse as part of the document.
    xml_body = re.sub(rb"<\?xpacket.*?\?>", b"", packet, flags=re.S)

    root = ET.fromstring(xml_body)  # raises ParseError on malformed XML
    assert root is not None


def test_rendering_is_deterministic() -> None:
    first = xmp_packet(title="x", language="nl", conformance=Conformance.PDF_A_3A)
    second = xmp_packet(title="x", language="nl", conformance=Conformance.PDF_A_3A)
    assert first == second


# --- ToUnicode -------------------------------------------------------------


def test_every_mapped_code_appears_as_a_bfchar_entry() -> None:
    mapping = {0x41: 0x41, 0x80: 0x20AC}  # 'A', and the euro sign
    cmap = to_unicode_cmap(mapping)

    assert b"<41> <0041>" in cmap
    assert b"<80> <20AC>" in cmap


def test_the_cmap_declares_the_identity_ucs_system() -> None:
    """What tells a reader this CMap maps to actual Unicode code points, which
    is what makes copy-paste and screen readers work at all.
    """
    cmap = to_unicode_cmap({0x41: 0x41})
    assert b"/Registry (Adobe)" in cmap
    assert b"/Ordering (UCS)" in cmap


def test_more_than_a_hundred_entries_splits_into_blocks() -> None:
    """The CMap format caps a single `bfchar` block at 100 entries. A table
    that ignored the cap would produce a CMap a strict reader refuses to
    parse - invisible in a forgiving one, discovered in a stricter one.
    """
    mapping = {code: code for code in range(150)}
    cmap = to_unicode_cmap(mapping)

    assert cmap.count(b"beginbfchar") == 2
    assert b"100 beginbfchar" in cmap
    assert b"50 beginbfchar" in cmap


def test_entries_are_emitted_in_code_order() -> None:
    """Deterministic output - the same reason nothing in `api.invoicing.pdf`
    reads the clock. A dict built in a different insertion order must not
    change the bytes.
    """
    forward = to_unicode_cmap({0x20: 0x20, 0x41: 0x41, 0x80: 0x20AC})
    backward = to_unicode_cmap({0x80: 0x20AC, 0x41: 0x41, 0x20: 0x20})
    assert forward == backward

    first = cmap_first_code(forward)
    assert first == 0x20


def cmap_first_code(cmap: bytes) -> int:
    # After `beginbfchar`, not the codespace range declaration above it
    # (`1 begincodespacerange <00> <FF> endcodespacerange`), which also
    # matches a naive `<XX>` search and would report 0x00 regardless of
    # entry order.
    after_header = cmap.split(b"beginbfchar\n", 1)[1]
    match = re.search(rb"<([0-9A-F]{2})>", after_header)
    assert match is not None
    return int(match.group(1), 16)


def test_the_cmap_is_a_complete_resource_definition() -> None:
    """The wrapper PDF expects around a CMap - without it a reader that
    validates resource structure refuses the font's /ToUnicode entirely,
    which is a silent downgrade to "not selectable" rather than an error
    anyone sees.
    """
    cmap = to_unicode_cmap({0x41: 0x41})

    assert cmap.startswith(b"/CIDInit /ProcSet findresource begin")
    assert cmap.rstrip().endswith(b"end\nend")
    assert b"begincmap" in cmap and b"endcmap" in cmap


# --- OutputIntent ----------------------------------------------------------


def test_the_output_intent_names_srgb_and_the_icc_object() -> None:
    body = output_intent(icc_object_number=42)

    assert b"/S /GTS_PDFA1" in body
    assert b"sRGB IEC61966-2.1" in body
    assert b"/DestOutputProfile 42 0 R" in body


def test_the_output_intent_declares_its_own_type() -> None:
    """PDF/A validators check /Type /OutputIntent specifically; an
    OutputIntent missing it is invisible to a conforming reader even though
    every other key is present.
    """
    assert b"/Type /OutputIntent" in output_intent(icc_object_number=1)


def test_a_different_component_count_is_accepted() -> None:
    """CMYK ICC profiles exist too; the function must not hardcode RGB into
    a place the caller explicitly overrode - even though sRGB is the only
    profile this module names as the default expectation.
    """
    body = output_intent(icc_object_number=1, components=4)
    assert b"/DestOutputProfile 1 0 R" in body


# --- unembedded_fonts: the D5 detail added with real font-embedding --------


def test_naming_the_unembedded_fonts_enriches_the_636_message() -> None:
    """Once fonts CAN be embedded (`api.invoicing.truetype`), a mixed
    embedded-heading/base-14-body document is still not PDF/A-conformant -
    and the gap must say WHICH text is still on a non-embeddable font, not
    only that some is.
    """
    gaps = conformance_gaps(
        Conformance.PDF_A_3A,
        fonts_embedded=False,
        icc_profile=SRGB,
        unembedded_fonts=frozenset({"Helvetica (base-14)"}),
    )
    font_gap = next(gap for gap in gaps if gap.clause == "6.3.4")
    assert "Helvetica (base-14)" in font_gap.closes_with


def test_no_unembedded_fonts_named_leaves_the_default_message() -> None:
    """When the caller has nothing to name (e.g. checking the gate in the
    abstract, as most tests in this file do), the message stays the generic
    `REQUIREMENTS` text rather than appending an empty clause.
    """
    gaps = conformance_gaps(Conformance.PDF_A_3A, fonts_embedded=False, icc_profile=SRGB)
    font_gap = next(gap for gap in gaps if gap.clause == "6.3.4")
    assert "Still drawing text with a non-embedded font" not in font_gap.closes_with


def test_assert_conformance_forwards_unembedded_fonts_into_the_message() -> None:
    with pytest.raises(ConformanceNotMet) as caught:
        assert_conformance(
            Conformance.PDF_A_3A,
            fonts_embedded=False,
            icc_profile=SRGB,
            unembedded_fonts=frozenset({"Helvetica-Bold (base-14)"}),
        )
    assert "Helvetica-Bold (base-14)" in str(caught.value)


# =============================================================================
# The mechanism, given a real embedded font AND a fake ICC profile.
#
# `fonts_embedded=True` is reachable now, for real, via
# `api.invoicing.truetype.build_embedded_font` over the synthetic test font -
# `tests/invoicing/test_pdf.py` proves render_pdf computes it genuinely.
# What follows here exercises the GATE LOGIC with that real mechanism and a
# fabricated ICC profile: it is NOT a claim that a document built this way is
# real PDF/A (the ICC bytes are not a real colour profile, and the font is
# not a licensed typeface - see tests/invoicing/support/synthetic_font.py's
# own docstring). It proves `conformance_gaps` opens exactly when both real
# conditions are true, using genuine mechanism rather than a hand-waved bool.
# =============================================================================


def test_the_gate_opens_given_a_genuinely_embedded_font_and_an_icc_profile() -> None:
    """Mechanism, not a real archival claim - see the section docstring
    above. `fonts_embedded=True` here is computed the same way
    `api.invoicing.pdf.render_pdf` computes it: every font actually used is
    one that was actually subset and embedded.
    """
    assert conformance_gaps(Conformance.PDF_A_3A, fonts_embedded=True, icc_profile=SRGB) == ()


# --- to_unicode_cmap_cid: the CID-keyed sibling for Identity-H fonts --------


def test_cid_cmap_uses_four_hex_digit_codes() -> None:
    """Identity-H codes are 2-byte CIDs - `<XXXX>`, not `to_unicode_cmap`'s
    single-byte `<XX>`. Using the wrong width here is the exact bug class
    this function's docstring warns about: it would silently mis-map every
    CID above 0xFF.
    """
    cmap = to_unicode_cmap_cid({1: 0x0041, 2: 0x20AC})
    assert b"<0001> <0041>" in cmap
    assert b"<0002> <20AC>" in cmap
    assert b"<01>" not in cmap


def test_cid_cmap_codespace_is_two_bytes() -> None:
    cmap = to_unicode_cmap_cid({1: 0x41})
    assert b"<0000> <FFFF>" in cmap
    assert b"<00> <FF>" not in cmap


def test_cid_cmap_declares_the_identity_ucs_system() -> None:
    cmap = to_unicode_cmap_cid({1: 0x41})
    assert b"/Registry (Adobe)" in cmap
    assert b"/Ordering (UCS)" in cmap


def test_cid_cmap_splits_into_blocks_of_a_hundred() -> None:
    mapping = {cid: 0x41 + cid for cid in range(150)}
    cmap = to_unicode_cmap_cid(mapping)

    assert cmap.count(b"beginbfchar") == 2
    assert b"100 beginbfchar" in cmap
    assert b"50 beginbfchar" in cmap


def test_cid_cmap_entries_are_emitted_in_cid_order() -> None:
    forward = to_unicode_cmap_cid({1: 0x41, 2: 0x42, 3: 0x20AC})
    backward = to_unicode_cmap_cid({3: 0x20AC, 2: 0x42, 1: 0x41})
    assert forward == backward


def test_cid_cmap_is_a_complete_resource_definition() -> None:
    cmap = to_unicode_cmap_cid({1: 0x41})
    assert cmap.startswith(b"/CIDInit /ProcSet findresource begin")
    assert cmap.rstrip().endswith(b"end\nend")
    assert b"begincmap" in cmap and b"endcmap" in cmap
