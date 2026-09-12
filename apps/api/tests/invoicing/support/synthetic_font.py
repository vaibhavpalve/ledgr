"""A minimal, HAND-BUILT TrueType font, for exercising `api.invoicing.
truetype`'s parser and subsetter.

**This is not a real typeface.** It exists solely to drive the sfnt/glyf/loca
binary-format code in `api.invoicing.truetype` with a font whose every byte
this test suite controls and can assert on - four glyphs, one of them
composite, just enough to prove parsing, subsetting (including composite-glyph
closure) and CIDFontType2 embedding round-trip correctly. It must NEVER be
embedded in a production document: it carries no licence, no design, and is
not fit to appear on an invoice. `api.invoicing.rendering` never imports this
module; only `tests/invoicing/test_truetype.py` (and any other test that needs
a real, parseable font binary) does.

--- The four glyphs ---

    0  .notdef             empty (no outline) - always retained
    1  'A'  (U+0041)       simple glyph, a triangle
    2  'B'  (U+0042)       simple glyph, a square
    3  (unmapped)          simple glyph, two small squares ("dieresis dots") -
                            reachable ONLY as a composite component, never
                            through `cmap` directly, which is what makes it
                            useful for proving composite-glyph closure
    4  'Ä'  (U+00C4)       COMPOSITE: glyph 1 ('A') plus glyph 3 (the dots),
                            offset upward - subsetting for U+00C4 alone must
                            still pull in glyphs 1 and 3, or the character
                            renders with a piece missing

`cmap` maps 0x41 -> 1, 0x42 -> 2, 0xC4 -> 4. Glyph 3 has no cmap entry.

`OS/2` is included with a serif family class, a bold weight class and no
italic bit, so `api.invoicing.truetype.parse_font`'s FontDescriptor-adjacent
fields (`is_serif`, `weight_class`, `cap_height`) have something real to read
rather than every field falling back to a default.
"""

from __future__ import annotations

import struct

from api.invoicing.truetype import build_sfnt

#: Codepoints this font actually maps, and to which glyph id - the "ground
#: truth" `test_truetype.py` checks `parse_font(...).cmap` against.
CODEPOINTS: dict[str, int] = {"A": 0x41, "B": 0x42, "Auml": 0xC4}
GLYPH_IDS: dict[str, int] = {".notdef": 0, "A": 1, "B": 2, "dieresis": 3, "Auml": 4}

_UNITS_PER_EM = 1000


def _simple_glyph(contours: list[list[tuple[int, int]]]) -> bytes:
    """One simple glyph: `contours`, each a closed polygon of ON-CURVE
    points. Deltas (both x and y) accumulate across the WHOLE glyph, not per
    contour - the one detail that is easy to get backwards when hand-writing
    this.
    """
    points = [point for contour in contours for point in contour]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    end_pts: list[int] = []
    index = -1
    for contour in contours:
        index += len(contour)
        end_pts.append(index)

    header = struct.pack(">hhhhh", len(contours), x_min, y_min, x_max, y_max)
    body = struct.pack(f">{len(end_pts)}H", *end_pts)
    body += struct.pack(">H", 0)  # instructionLength

    # ON_CURVE_POINT (bit 0) for every point; no repeat-flag compression and
    # no short-vector encoding, since these glyphs are small enough that
    # plain int16 deltas cost nothing worth optimising.
    body += bytes([0x01] * len(points))

    prev_x = prev_y = 0
    dxs = bytearray()
    dys = bytearray()
    for x, y in points:
        dxs += struct.pack(">h", x - prev_x)
        dys += struct.pack(">h", y - prev_y)
        prev_x, prev_y = x, y
    return header + body + bytes(dxs) + bytes(dys)


#: Component flags (ISO/IEC 14496-22 / OpenType `glyf` composite glyphs).
_ARGS_ARE_XY_VALUES = 0x0002
_ARG_1_AND_2_ARE_WORDS = 0x0001
_MORE_COMPONENTS = 0x0020


def _composite_glyph(components: list[tuple[int, int, int]]) -> bytes:
    """`components` is `(glyph_id, dx, dy)` triples. Bounding box is a fixed
    generous box rather than computed - a composite's header bbox is
    advisory and nothing here reads it back.
    """
    header = struct.pack(">hhhhh", -1, 0, 0, 700, 900)
    body = bytearray()
    for index, (glyph_id, dx, dy) in enumerate(components):
        is_last = index == len(components) - 1
        flags = _ARGS_ARE_XY_VALUES | _ARG_1_AND_2_ARE_WORDS
        if not is_last:
            flags |= _MORE_COMPONENTS
        body += struct.pack(">HH", flags, glyph_id)
        body += struct.pack(">hh", dx, dy)
    return bytes(header) + bytes(body)


def _cmap_format4(mapping: dict[int, int]) -> bytes:
    """A minimal, single-subtable `cmap` (format 4, platform 3 / encoding 1 -
    Windows BMP), covering exactly `mapping` plus the mandatory terminator
    segment.
    """
    segments = sorted(mapping.items()) + [(0xFFFF, None)]
    seg_count = len(segments)

    end_codes: list[int] = []
    start_codes: list[int] = []
    id_deltas: list[int] = []
    for code, glyph_id in segments:
        end_codes.append(code)
        start_codes.append(code)
        if glyph_id is None:
            id_deltas.append(1)  # the terminator: delta is conventionally 1
        else:
            delta = (glyph_id - code) % 0x10000
            if delta >= 0x8000:  # two's-complement: store as the signed int16 it represents
                delta -= 0x10000
            id_deltas.append(delta)

    seg_count_x2 = seg_count * 2
    search_range = 2 * (1 << (seg_count.bit_length() - 1)) if seg_count else 0
    entry_selector = seg_count.bit_length() - 1 if seg_count else 0
    range_shift = seg_count_x2 - search_range

    subtable = struct.pack(">HHH", 4, 0, 0)  # format, length placeholder, language
    subtable += struct.pack(">HHHH", seg_count_x2, search_range, entry_selector, range_shift)
    subtable += struct.pack(f">{seg_count}H", *end_codes)
    subtable += struct.pack(">H", 0)  # reservedPad
    subtable += struct.pack(f">{seg_count}H", *start_codes)
    subtable += struct.pack(f">{seg_count}h", *id_deltas)
    subtable += struct.pack(f">{seg_count}H", *([0] * seg_count))  # idRangeOffset: all 0

    # `length` sits at bytes [2:4] of the subtable itself; patch it in place
    # now that the real length is known (the format+placeholder pair being
    # replaced is exactly 4 bytes, the same size as what replaces it, so the
    # total length does not shift).
    subtable = struct.pack(">HH", 4, len(subtable)) + subtable[4:]

    header = struct.pack(">HH", 0, 1)  # version, numTables
    header += struct.pack(">HHI", 3, 1, 4 + 8)  # platformID, encodingID, offset
    return header + subtable


def _os2(
    *, weight_class: int, is_bold: bool, family_class: int, cap_height: int, sx_height: int
) -> bytes:
    fs_selection = 0x0020 if is_bold else 0x0000
    table = bytearray(96)
    struct.pack_into(">H", table, 0, 2)  # version 2 - carries sCapHeight/sxHeight
    struct.pack_into(">H", table, 4, weight_class)
    struct.pack_into(">h", table, 30, family_class << 8)
    struct.pack_into(">H", table, 62, fs_selection)
    struct.pack_into(">h", table, 86, sx_height)
    struct.pack_into(">h", table, 88, cap_height)
    return bytes(table)


def _post() -> bytes:
    """Version 3 (no glyph names) - `italicAngle` 0, not fixed-pitch."""
    return struct.pack(">iiHHIIIII", 0x00030000, 0, 0, 0, 0, 0, 0, 0, 0)


def build_synthetic_font() -> bytes:
    """The whole font binary - `head`/`hhea`/`maxp`/`hmtx`/`loca`/`glyf`/
    `cmap`/`OS/2`/`post`, assembled with `api.invoicing.truetype.build_sfnt`
    (the SAME sfnt writer the production subsetter uses, exercised here from
    the "build a whole font" direction rather than the "build a subset"
    direction).
    """
    glyph_a = _simple_glyph([[(0, 0), (500, 0), (250, 600)]])
    glyph_b = _simple_glyph([[(0, 0), (500, 0), (500, 500), (0, 500)]])
    glyph_dieresis = _simple_glyph(
        [[(100, 0), (200, 0), (200, 100), (100, 100)], [(300, 0), (400, 0), (400, 100), (300, 100)]]
    )
    glyph_notdef = _simple_glyph([[(0, 0), (0, 0), (0, 0)]])  # degenerate but present
    glyph_auml = _composite_glyph([(GLYPH_IDS["A"], 0, 0), (GLYPH_IDS["dieresis"], 100, 650)])

    glyphs = [glyph_notdef, glyph_a, glyph_b, glyph_dieresis, glyph_auml]
    glyf = bytearray()
    loca = [0]
    hmtx = bytearray()
    advances = [500, 600, 650, 600, 700]
    for glyph, advance in zip(glyphs, advances, strict=True):
        padded = glyph + (b"\x00" if len(glyph) % 2 else b"")
        glyf += padded
        loca.append(loca[-1] + len(padded))
        hmtx += struct.pack(">Hh", advance, 40)

    num_glyphs = len(glyphs)
    loca_bytes = b"".join(struct.pack(">I", offset) for offset in loca)

    # Built as separate, individually-verifiable field groups rather than one
    # long format string - a single miscounted letter in a 17-field format
    # string is exactly the kind of off-by-one this avoids by construction.
    head = b"".join(
        (
            struct.pack(">HH", 1, 0),  # majorVersion, minorVersion
            struct.pack(">i", 0x00010000),  # fontRevision (Fixed 1.0)
            struct.pack(">I", 0),  # checkSumAdjustment - build_sfnt fills this in
            struct.pack(">I", 0x5F0F3CF5),  # magicNumber
            struct.pack(">H", 0),  # flags
            struct.pack(">H", _UNITS_PER_EM),
            struct.pack(">qq", 0, 0),  # created, modified - 0 for determinism
            struct.pack(">hhhh", 0, 0, 700, 900),  # xMin, yMin, xMax, yMax
            struct.pack(">H", 0),  # macStyle
            struct.pack(">H", 8),  # lowestRecPPEM
            struct.pack(">h", 2),  # fontDirectionHint (deprecated; conventionally 2)
            struct.pack(">h", 1),  # indexToLocFormat: long
            struct.pack(">h", 0),  # glyphDataFormat
        )
    )
    assert len(head) == 54, f"head table must be exactly 54 bytes, got {len(head)}"

    hhea = b"".join(
        (
            struct.pack(">HH", 1, 0),  # majorVersion, minorVersion
            struct.pack(">hhh", 900, -200, 0),  # ascender, descender, lineGap
            struct.pack(">H", 1000),  # advanceWidthMax
            struct.pack(">hhh", 0, 0, 900),  # minLSB, minRSB, xMaxExtent
            struct.pack(">hhh", 1, 0, 0),  # caretSlopeRise, caretSlopeRun, caretOffset
            struct.pack(">hhhh", 0, 0, 0, 0),  # reserved x4
            struct.pack(">h", 0),  # metricDataFormat
            struct.pack(">H", num_glyphs),  # numberOfHMetrics
        )
    )
    assert len(hhea) == 36, f"hhea table must be exactly 36 bytes, got {len(hhea)}"
    maxp = struct.pack(">IH", 0x00005000, num_glyphs)  # version 0.5: just numGlyphs
    cmap = _cmap_format4(
        {
            CODEPOINTS["A"]: GLYPH_IDS["A"],
            CODEPOINTS["B"]: GLYPH_IDS["B"],
            CODEPOINTS["Auml"]: GLYPH_IDS["Auml"],
        }
    )
    os2 = _os2(weight_class=700, is_bold=True, family_class=2, cap_height=700, sx_height=500)
    post = _post()

    return build_sfnt(
        {
            b"head": head,
            b"hhea": hhea,
            b"maxp": maxp,
            b"hmtx": bytes(hmtx),
            b"loca": loca_bytes,
            b"glyf": bytes(glyf),
            b"cmap": cmap,
            b"OS/2": os2,
            b"post": post,
        }
    )
