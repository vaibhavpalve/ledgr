"""api.invoicing.truetype - the TrueType/CIDFontType2 embedding mechanism
behind PDF/A-3's font-embedding requirement (ISO 19005-3 6.3.4).

Exercised entirely against `tests/invoicing/support/synthetic_font.py`'s
hand-built, four-glyph, NOT-A-REAL-TYPEFACE fixture - see that module's
docstring for exactly what it is and is not. Nothing here claims PDF/A
conformance; that is `test_pdfa.py`'s and `test_pdf.py`'s job.
"""

from __future__ import annotations

import pytest

from api.invoicing.truetype import (
    EmbeddedFont,
    TrueTypeError,
    build_embedded_font,
    parse_font,
)
from tests.invoicing.support.synthetic_font import CODEPOINTS, GLYPH_IDS, build_synthetic_font

FONT_BYTES = build_synthetic_font()


# --- parsing -------------------------------------------------------------


def test_the_font_parses_without_error() -> None:
    font = parse_font(FONT_BYTES)
    assert font.num_glyphs == 5


def test_units_per_em_is_read() -> None:
    assert parse_font(FONT_BYTES).units_per_em == 1000


def test_the_cmap_maps_every_codepoint_to_its_glyph() -> None:
    font = parse_font(FONT_BYTES)
    assert font.cmap[CODEPOINTS["A"]] == GLYPH_IDS["A"]
    assert font.cmap[CODEPOINTS["B"]] == GLYPH_IDS["B"]
    assert font.cmap[CODEPOINTS["Auml"]] == GLYPH_IDS["Auml"]


def test_the_unmapped_dieresis_glyph_has_no_cmap_entry() -> None:
    """Glyph 3 is reachable only as a composite component - proving the
    fixture actually exercises composite closure rather than every glyph
    being directly cmapped (in which case closure would never be tested).
    """
    font = parse_font(FONT_BYTES)
    assert GLYPH_IDS["dieresis"] not in font.cmap.values()


def test_hmtx_advance_widths_are_read_per_glyph() -> None:
    font = parse_font(FONT_BYTES)
    assert font.hmtx[GLYPH_IDS["A"]][0] == 600
    assert font.hmtx[GLYPH_IDS["B"]][0] == 650
    assert font.hmtx[GLYPH_IDS["Auml"]][0] == 700


def test_ascender_and_descender_are_read() -> None:
    font = parse_font(FONT_BYTES)
    assert font.ascender == 900
    assert font.descender == -200


def test_os2_weight_and_serif_and_cap_height_are_read() -> None:
    font = parse_font(FONT_BYTES)
    assert font.weight_class == 700
    assert font.is_bold is True
    assert font.is_serif is True
    assert font.cap_height == 700


def test_post_italic_angle_is_zero_and_not_fixed_pitch() -> None:
    font = parse_font(FONT_BYTES)
    assert font.italic_angle == 0.0
    assert font.is_fixed_pitch is False


def test_an_unrecognised_sfnt_tag_is_refused_by_name() -> None:
    with pytest.raises(TrueTypeError, match="unrecognised font format"):
        parse_font(b"NOPE" + b"\x00" * 20)


def test_a_cff_flavoured_opentype_font_is_refused_by_name() -> None:
    """`OTTO` is a real, valid sfnt tag - just for the wrong glyph format
    (PostScript outlines). Refused BY NAME, not as a generic parse failure,
    because the fix is "use a TrueType-outline font", not "the file is
    corrupt".
    """
    with pytest.raises(TrueTypeError, match="CFF-flavoured"):
        parse_font(b"OTTO" + b"\x00" * 20)


def test_a_truncated_file_is_refused_rather_than_crashing() -> None:
    with pytest.raises(TrueTypeError):
        parse_font(FONT_BYTES[:20])


def test_a_font_missing_a_required_table_is_refused_by_name() -> None:
    """`build_sfnt` from a hand-picked subset of tables, omitting `hmtx` -
    proves the specific missing-table message names the table, not just
    "malformed font".
    """
    from api.invoicing.truetype import build_sfnt

    font = parse_font(FONT_BYTES)
    # A structurally valid (if empty) loca table sized for the real glyph
    # count, so the missing table this test is actually about - hmtx - is
    # what `parse_font` trips on, not an incidentally-mismatched loca.
    empty_loca = b"\x00\x00\x00\x00" * (font.num_glyphs + 1)
    incomplete = build_sfnt(
        {
            b"head": font.head_bytes,
            b"hhea": font.hhea_bytes,
            b"maxp": font.maxp_bytes,
            # hmtx deliberately omitted
            b"loca": empty_loca,
            b"glyf": b"",
        }
    )
    with pytest.raises(TrueTypeError, match="'hmtx'"):
        parse_font(incomplete)


# --- subsetting ------------------------------------------------------------


def _embed(codepoints: set[int]) -> EmbeddedFont:
    font = parse_font(FONT_BYTES)
    return build_embedded_font(
        font, resource_name=b"F3", base_font_name=b"SyntheticTest", codepoints=codepoints
    )


def test_notdef_is_always_glyph_zero_in_the_subset() -> None:
    embedded = _embed({CODEPOINTS["A"]})
    # CID 0 must exist (it is the retained .notdef) even though .notdef has
    # no cmap entry of its own - it is added unconditionally, not derived
    # from `codepoints`.
    assert 0 in embedded.widths


def test_subsetting_for_one_letter_excludes_the_other() -> None:
    """A genuine subset: asking for 'A' alone must not smuggle in 'B'."""
    embedded = _embed({CODEPOINTS["A"]})
    assert embedded.codepoint_to_cid.keys() == {CODEPOINTS["A"]}


def test_composite_glyph_closure_pulls_in_every_component() -> None:
    """The load-bearing property: subsetting for U+00C4 ('Ä') ALONE must
    still retain glyph 1 ('A') and glyph 3 (the dieresis dots) it is built
    from, even though neither is directly requested - losing either one
    means the character renders with a piece missing.
    """
    embedded = _embed({CODEPOINTS["Auml"]})
    # Three real glyphs must be present in the subset: .notdef, the
    # component 'A', the component dieresis, and the composite itself - the
    # CIDs are new and sequential, but there must be exactly 4 of them.
    assert len(embedded.widths) == 4


def test_two_requested_codepoints_still_only_subset_what_is_reachable() -> None:
    """Requesting 'A' and 'B' together must NOT drag in the dieresis glyph -
    proving closure only follows COMPOSITE references, not "everything in
    the font."
    """
    embedded = _embed({CODEPOINTS["A"], CODEPOINTS["B"]})
    # .notdef + A + B = 3 glyphs; the dieresis and the composite 'Ä' are both
    # unreached and must be excluded.
    assert len(embedded.widths) == 3


def test_new_glyph_ids_are_sequential_starting_at_zero() -> None:
    embedded = _embed({CODEPOINTS["A"], CODEPOINTS["B"]})
    assert sorted(embedded.widths.keys()) == list(range(len(embedded.widths)))


def test_widths_are_scaled_to_1000_units_per_em() -> None:
    """This fixture's `unitsPerEm` is already 1000, so the scale factor is 1
    and widths pass through unchanged - the property under test is that NO
    scaling corruption happens at scale=1, which is the case a font shipped
    at a different (e.g. 2048) unitsPerEm would multiply out from.
    """
    embedded = _embed({CODEPOINTS["A"]})
    cid = embedded.codepoint_to_cid[CODEPOINTS["A"]]
    assert embedded.widths[cid] == 600


# --- ToUnicode / encoding ----------------------------------------------------


def test_cid_to_unicode_round_trips_through_the_subset() -> None:
    embedded = _embed({CODEPOINTS["A"], CODEPOINTS["B"]})
    cid_a = embedded.codepoint_to_cid[CODEPOINTS["A"]]
    assert embedded.cid_to_unicode[cid_a] == CODEPOINTS["A"]


def test_encode_produces_two_bytes_per_character() -> None:
    embedded = _embed({CODEPOINTS["A"], CODEPOINTS["B"]})
    encoded = embedded.encode("AB")
    assert len(encoded) == 4


def test_encode_uses_the_cid_not_the_original_glyph_id() -> None:
    """Identity-H's codes are CIDs, and CIDToGIDMap is Identity - so what
    `encode` emits must be the NEW (subset) id, not the original font's
    glyph id, whenever the two differ (which subsetting for 'B' alone
    guarantees, since 'B' was originally glyph 2 but becomes CID 1 once
    glyph 0/.notdef and 'B' are the only two glyphs kept).
    """
    embedded = _embed({CODEPOINTS["B"]})
    cid = embedded.codepoint_to_cid[CODEPOINTS["B"]]
    assert cid != GLYPH_IDS["B"]
    assert embedded.encode("B") == cid.to_bytes(2, "big")


def test_encode_refuses_a_character_outside_the_subset() -> None:
    embedded = _embed({CODEPOINTS["A"]})
    with pytest.raises(TrueTypeError, match="not among the code points"):
        embedded.encode("B")


def test_supports_reports_coverage_without_raising() -> None:
    embedded = _embed({CODEPOINTS["A"]})
    assert embedded.supports("A") is True
    assert embedded.supports("B") is False


# --- FontDescriptor fields ----------------------------------------------------


def test_font_bbox_is_computed_from_the_head_table() -> None:
    embedded = _embed({CODEPOINTS["A"]})
    x_min, y_min, x_max, y_max = embedded.font_bbox
    assert (x_min, y_min, x_max, y_max) == (0, 0, 700, 900)


def test_ascent_and_descent_come_from_hhea() -> None:
    embedded = _embed({CODEPOINTS["A"]})
    assert embedded.ascent == 900
    assert embedded.descent == -200


def test_cap_height_comes_from_os2_when_present() -> None:
    embedded = _embed({CODEPOINTS["A"]})
    assert embedded.cap_height == 700


def test_flags_mark_serif_and_force_bold_from_the_source_font() -> None:
    embedded = _embed({CODEPOINTS["A"]})
    assert embedded.flags & (1 << 1)  # Serif
    assert embedded.flags & (1 << 18)  # ForceBold (weight_class 700 >= 600)
    assert embedded.flags & (1 << 2)  # Symbolic - always set, see build_embedded_font


def test_stem_v_is_a_deterministic_function_of_weight_class() -> None:
    embedded = _embed({CODEPOINTS["A"]})
    assert embedded.stem_v == 50 + round((700 / 65.0) ** 2)


def test_base_font_name_carries_a_deterministic_subset_tag() -> None:
    """PDF's `ABCDEF+Name` convention, but the tag itself must be
    deterministic (FR-TPL-017 / `api.invoicing.pdf`'s determinism guarantee) -
    the same codepoints subset twice must produce the identical tag, not a
    fresh random one each time.
    """
    first = _embed({CODEPOINTS["A"]})
    second = _embed({CODEPOINTS["A"]})
    assert first.base_font_name == second.base_font_name
    assert first.base_font_name.endswith(b"+SyntheticTest")
    tag = first.base_font_name.split(b"+")[0]
    assert len(tag) == 6
    assert all(65 <= byte <= 90 for byte in tag)  # A-Z


def test_a_different_subset_of_the_same_font_gets_a_different_tag() -> None:
    a_only = _embed({CODEPOINTS["A"]})
    a_and_b = _embed({CODEPOINTS["A"], CODEPOINTS["B"]})
    assert a_only.base_font_name != a_and_b.base_font_name


# --- sfnt re-parseability ------------------------------------------------


def test_the_subset_font_program_is_itself_a_valid_parseable_sfnt() -> None:
    """Not a claim this subset is INSTALLABLE (it deliberately omits `cmap`,
    `name` and `post` - see `api.invoicing.truetype`'s module docstring) but
    it must still be a well-formed enough sfnt that a table-directory walk
    (this project's own `parse_font`, applied to the OUTPUT this time) finds
    `head`/`hhea`/`maxp`/`hmtx`/`loca`/`glyf` at the offsets its own
    directory claims.
    """
    embedded = _embed({CODEPOINTS["Auml"]})

    # `parse_font` requires a `cmap` table, which the subset does not carry
    # (see the module docstring on why that is correct for this PDF usage) -
    # so this test reads the table directory directly instead of going
    # through the full parser.
    from api.invoicing.truetype import (
        _read_table_directory,  # noqa: SLF001 - test-only introspection
    )

    tables = _read_table_directory(embedded.font_program)
    assert set(tables) == {b"head", b"hhea", b"maxp", b"hmtx", b"loca", b"glyf"}

    head_offset, head_length = tables[b"head"]
    assert head_length == 54
    assert embedded.font_program[head_offset : head_offset + 54]


def test_the_subset_checksum_adjustment_makes_the_whole_file_checksum_to_the_magic_constant() -> (
    None
):
    """The one property that proves `checkSumAdjustment` was computed
    correctly rather than left as a placeholder 0 - per spec, summing every
    4-byte word of the whole file (padded) must equal `0xB1B0AFBA`.
    """
    embedded = _embed({CODEPOINTS["A"]})
    data = embedded.font_program
    padded = data + b"\x00" * (-len(data) % 4)
    total = 0
    for index in range(0, len(padded), 4):
        total = (total + int.from_bytes(padded[index : index + 4], "big")) & 0xFFFFFFFF
    assert total == 0xB1B0AFBA
