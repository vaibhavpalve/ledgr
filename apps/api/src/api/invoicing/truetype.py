"""Hand-rolled OpenType/TrueType parsing, subsetting and CIDFontType2 embedding
- the mechanism behind PDF/A-3's one remaining gap (`api.invoicing.pdfa`
clause 6.3.4).

--- Why hand-written, again ---

`api.invoicing.pdf`'s own docstring explains why this codebase writes PDF
bytes by hand rather than reaching for a library: every PDF library brings a
rendering engine, font subsetting and an image pipeline it does not want. A
TrueType/OpenType parser is the natural continuation of that stance, not an
exception to it - fontTools and its relatives are excellent and are exactly
the kind of dependency surface (a general-purpose font *editor*, hinting
instructions, CFF outlines, variable-font axes, an entire OpenType layout
engine) this project keeps out for a document that only ever draws Latin text
in one weight at a time.

--- What this module does and does not parse ---

Only the TrueType-OUTLINE flavour of OpenType (`glyf`/`loca`), matching
`pdf.py`'s WinAnsi/Latin-1 ambitions - `head`, `hhea`, `hmtx`, `maxp`, `cmap`
(formats 0, 4, 6 and 12), `loca`, `glyf`, `OS/2` and the four fixed fields of
`post` it needs (Flags/ItalicAngle, never glyph names - a `CIDFontType2`
embedding has no use for them). A CFF-flavoured OpenType font (`OTTO`,
PostScript outlines) is refused by name: it is a different glyph format this
parser does not read, not a corrupt file.

--- Subsetting, honestly ---

`build_embedded_font` takes the exact set of Unicode code points a document's
text actually draws in one typeface and returns exactly those glyphs, PLUS
every glyph reachable through a composite reference (an accented letter built
from two component outlines must not lose one - see `_closure`), renumbered
to new, sequential glyph ids starting at 0 (`.notdef`, always retained). That
is what makes this genuine subsetting rather than "embed the whole font and
call it a subset": a deployment's licensed font file might carry thousands of
glyphs across scripts this platform never draws, and none of them ends up in
the PDF.

`CIDToGIDMap` is `/Identity` throughout - see `_build_subset`: the new glyph
ids ARE the CIDs, by construction, so there is no separate mapping table to
build, store or get wrong.

--- What the subset font program does NOT carry, and why that is enough ---

Six tables: `head`, `hhea`, `hmtx`, `maxp`, `loca`, `glyf`. No `cmap`, no
`name`, no `post`, no `OS/2`. For an *installable* system font that would be
invalid; for a `FontFile2` embedded under a `CIDFontType2` with `/Encoding
/Identity-H` and `/CIDToGIDMap /Identity`, it is complete. Identity-H's codes
ARE the CIDs, CIDToGIDMap Identity says CIDs ARE glyph ids, so nothing in this
pipeline ever asks the font program itself "which glyph is U+00C4" - the PDF
content stream already says "glyph 7" directly, and the font program's own
`cmap` (if it had one) would never be consulted. FR-TPL-016's ToUnicode
CMap - the thing that makes text SELECTABLE and readable by a screen reader -
is a PDF-level object built from `codepoint_to_cid`/`cid_to_unicode` below and
`api.invoicing.pdfa.to_unicode_cmap_cid`, entirely independent of whether the
font program itself carries one.

`FontDescriptor` fields (`Flags`, `FontBBox`, `ItalicAngle`, `Ascent`,
`Descent`, `CapHeight`, `StemV`) are computed from the SOURCE font's tables
before subsetting, never invented - except `StemV`, which TrueType has no
field for at all. That one is the industry-standard `(usWeightClass / 65)^2 +
50` approximation every mainstream subsetter uses for exactly this reason;
`_stem_v` names it as a heuristic rather than presenting it as a measurement.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

__all__ = [
    "TrueTypeError",
    "TrueTypeFont",
    "parse_font",
    "EmbeddedFont",
    "build_embedded_font",
    "build_sfnt",
]


class TrueTypeError(ValueError):
    """A font binary this module cannot parse or embed.

    Every raise names what was expected, for the same D5 reason
    `api.invoicing.pdf.ImageError` does: the person who meets one is a
    deployment operator supplying FR-TPL-002's licensed font file, not an end
    user, but "unsupported font" is still a dead end without a next action.
    """


# =============================================================================
# Parsing
# =============================================================================

#: IBM font-class high byte (OS/2 `sFamilyClass` >> 8) values that are serifed
#: designs - oldstyle, transitional, modern (Didone), clarendon/slab and
#: freeform serifs. Sans-serif (8), script (10) and symbolic/other (0, 12, 13)
#: are deliberately excluded.
_SERIF_FAMILY_CLASSES = frozenset({1, 2, 3, 4, 5, 7})

_COMPOSITE_ARG_WORDS = 0x0001
_COMPOSITE_HAVE_SCALE = 0x0008
_COMPOSITE_MORE_COMPONENTS = 0x0020
_COMPOSITE_XY_SCALE = 0x0040
_COMPOSITE_TWO_BY_TWO = 0x0080


@dataclass(frozen=True, slots=True)
class TrueTypeFont:
    """A parsed OpenType/TrueType (outline-flavoured) font, ready to subset.

    Carries the raw `head`/`hhea`/`maxp` table bytes alongside the fields this
    module reads out of them, so `_build_subset` can clone-and-patch the
    ORIGINAL tables (preserving every field it has no opinion on - creation
    flags, glyph-count hints, and so on) rather than reconstructing them from
    scratch.
    """

    units_per_em: int
    num_glyphs: int
    loca: tuple[int, ...]
    glyf: bytes
    hmtx: tuple[tuple[int, int], ...]
    #: Unicode code point -> glyph id, from whichever `cmap` subtable this
    #: font offers that this module understands - see `_parse_cmap`.
    cmap: dict[int, int]
    ascender: int
    descender: int
    x_min: int
    y_min: int
    x_max: int
    y_max: int
    weight_class: int
    is_italic: bool
    is_bold: bool
    is_fixed_pitch: bool
    is_serif: bool
    italic_angle: float
    #: None when the font's `OS/2` table is absent or too old (version < 2) to
    #: carry it - `build_embedded_font` falls back to an estimate from the
    #: ascender in that case, clearly as a fallback rather than a measurement.
    cap_height: int | None
    head_bytes: bytes
    hhea_bytes: bytes
    maxp_bytes: bytes


def _read_table_directory(data: bytes) -> dict[bytes, tuple[int, int]]:
    if len(data) < 12:
        raise TrueTypeError("this file is too short to be a TrueType or OpenType font")
    sfnt_version = data[0:4]
    if sfnt_version not in (b"\x00\x01\x00\x00", b"true", b"typ1"):
        if sfnt_version == b"OTTO":
            raise TrueTypeError(
                "this is a CFF-flavoured OpenType font (PostScript outlines, "
                "'OTTO'). This module only parses TrueType-outline fonts "
                "('glyf'/'loca'); supply a .ttf, or the TrueType-outline "
                "variant of the typeface."
            )
        raise TrueTypeError(f"unrecognised font format (sfnt tag {sfnt_version!r})")

    (num_tables,) = struct.unpack(">H", data[4:6])
    tables: dict[bytes, tuple[int, int]] = {}
    offset = 12
    for _ in range(num_tables):
        if offset + 16 > len(data):
            raise TrueTypeError("this font's table directory is truncated")
        tag = data[offset : offset + 4]
        table_offset, length = struct.unpack(">II", data[offset + 8 : offset + 16])
        tables[tag] = (table_offset, length)
        offset += 16
    return tables


def parse_font(data: bytes) -> TrueTypeFont:
    """Parse an OpenType/TrueType-outline font binary.

    Raises `TrueTypeError` naming what is missing or unsupported, rather than
    a bare `struct.error` or `KeyError` - the same D5 posture every refusal in
    this codebase takes.
    """
    tables = _read_table_directory(data)

    def table(tag: bytes) -> bytes:
        if tag not in tables:
            raise TrueTypeError(
                f"this font has no {tag.decode('ascii', 'replace')!r} table and cannot be embedded"
            )
        offset, length = tables[tag]
        return data[offset : offset + length]

    head = table(b"head")
    if len(head) < 54:
        raise TrueTypeError("this font's head table is too short; it may be damaged")
    (units_per_em,) = struct.unpack(">H", head[18:20])
    x_min, y_min, x_max, y_max = struct.unpack(">hhhh", head[36:44])
    mac_style: int
    (mac_style,) = struct.unpack(">H", head[44:46])
    (index_to_loc_format,) = struct.unpack(">h", head[50:52])

    hhea = table(b"hhea")
    if len(hhea) < 36:
        raise TrueTypeError("this font's hhea table is too short; it may be damaged")
    ascender, descender = struct.unpack(">hh", hhea[4:8])
    (number_of_h_metrics,) = struct.unpack(">H", hhea[34:36])

    maxp = table(b"maxp")
    if len(maxp) < 6:
        raise TrueTypeError("this font's maxp table is too short; it may be damaged")
    (num_glyphs,) = struct.unpack(">H", maxp[4:6])

    loca = _parse_loca(
        table(b"loca"), index_to_loc_format=index_to_loc_format, num_glyphs=num_glyphs
    )
    glyf = table(b"glyf")
    hmtx = _parse_hmtx(
        table(b"hmtx"), number_of_h_metrics=number_of_h_metrics, num_glyphs=num_glyphs
    )
    cmap = _parse_cmap(table(b"cmap"))

    weight_class = 400
    is_italic = bool(mac_style & 0x0002)
    is_bold = bool(mac_style & 0x0001)
    is_serif = False
    cap_height: int | None = None
    if b"OS/2" in tables:
        weight_class, is_italic, is_bold, is_serif, cap_height = _parse_os2(table(b"OS/2"))

    is_fixed_pitch = False
    italic_angle = 0.0
    if b"post" in tables:
        post = table(b"post")
        if len(post) >= 32:
            italic_angle = _fixed_to_float(struct.unpack(">i", post[4:8])[0])
            is_fixed_pitch = struct.unpack(">I", post[12:16])[0] != 0

    return TrueTypeFont(
        units_per_em=units_per_em or 1000,
        num_glyphs=num_glyphs,
        loca=loca,
        glyf=glyf,
        hmtx=hmtx,
        cmap=cmap,
        ascender=ascender,
        descender=descender,
        x_min=x_min,
        y_min=y_min,
        x_max=x_max,
        y_max=y_max,
        weight_class=weight_class,
        is_italic=is_italic,
        is_bold=is_bold,
        is_fixed_pitch=is_fixed_pitch,
        is_serif=is_serif,
        italic_angle=italic_angle,
        cap_height=cap_height,
        head_bytes=head,
        hhea_bytes=hhea,
        maxp_bytes=maxp,
    )


def _parse_loca(raw: bytes, *, index_to_loc_format: int, num_glyphs: int) -> tuple[int, ...]:
    if index_to_loc_format == 0:
        count = len(raw) // 2
        if count != num_glyphs + 1:
            raise TrueTypeError("this font's loca table does not match its glyph count")
        return tuple(v * 2 for v in struct.unpack(f">{count}H", raw[: count * 2]))
    count = len(raw) // 4
    if count != num_glyphs + 1:
        raise TrueTypeError("this font's loca table does not match its glyph count")
    return struct.unpack(f">{count}I", raw[: count * 4])


def _parse_hmtx(
    raw: bytes, *, number_of_h_metrics: int, num_glyphs: int
) -> tuple[tuple[int, int], ...]:
    if number_of_h_metrics == 0 or number_of_h_metrics > num_glyphs:
        raise TrueTypeError("this font's hhea table declares an impossible number of h-metrics")

    entries: list[tuple[int, int]] = []
    offset = 0
    last_advance = 0
    for _ in range(number_of_h_metrics):
        advance, lsb = struct.unpack(">Hh", raw[offset : offset + 4])
        entries.append((advance, lsb))
        last_advance = advance
        offset += 4
    for _ in range(num_glyphs - number_of_h_metrics):
        (lsb,) = struct.unpack(">h", raw[offset : offset + 2])
        entries.append((last_advance, lsb))
        offset += 2
    return tuple(entries)


#: Priority order for choosing a `cmap` subtable when several are present -
#: full-Unicode formats first, then BMP, then legacy 1-byte tables. Value is a
#: sort key; lower wins. Anything not listed sorts after every listed pair.
_CMAP_PRIORITY: dict[tuple[int, int], int] = {
    (3, 10): 0,
    (0, 6): 0,
    (0, 4): 0,
    (3, 1): 1,
    (0, 3): 1,
    (0, 2): 1,
    (0, 1): 1,
    (0, 0): 1,
    (1, 0): 2,
}


def _parse_cmap(raw: bytes) -> dict[int, int]:
    if len(raw) < 4:
        raise TrueTypeError("this font's cmap table is too short; it may be damaged")
    (num_tables,) = struct.unpack(">H", raw[2:4])
    records: list[tuple[int, int, int]] = []
    offset = 4
    for _ in range(num_tables):
        platform_id, encoding_id, subtable_offset = struct.unpack(">HHI", raw[offset : offset + 8])
        records.append((platform_id, encoding_id, subtable_offset))
        offset += 8
    if not records:
        raise TrueTypeError("this font's cmap table has no encoding records")

    records.sort(key=lambda record: _CMAP_PRIORITY.get((record[0], record[1]), 3))

    for _platform_id, _encoding_id, subtable_offset in records:
        (subtable_format,) = struct.unpack(">H", raw[subtable_offset : subtable_offset + 2])
        if subtable_format == 4:
            return _parse_cmap_format4(raw, subtable_offset)
        if subtable_format == 12:
            return _parse_cmap_format12(raw, subtable_offset)
        if subtable_format == 6:
            return _parse_cmap_format6(raw, subtable_offset)
        if subtable_format == 0:
            return _parse_cmap_format0(raw, subtable_offset)

    raise TrueTypeError(
        "this font's cmap table uses only subtable formats this module does not "
        "read (supported: 0, 4, 6, 12)"
    )


def _parse_cmap_format0(raw: bytes, offset: int) -> dict[int, int]:
    glyph_ids = raw[offset + 6 : offset + 6 + 256]
    return {code: gid for code, gid in enumerate(glyph_ids) if gid != 0}


def _parse_cmap_format6(raw: bytes, offset: int) -> dict[int, int]:
    first_code, entry_count = struct.unpack(">HH", raw[offset + 6 : offset + 10])
    ids = struct.unpack(f">{entry_count}H", raw[offset + 10 : offset + 10 + entry_count * 2])
    return {first_code + index: gid for index, gid in enumerate(ids) if gid != 0}


def _parse_cmap_format4(raw: bytes, offset: int) -> dict[int, int]:
    (seg_count_x2,) = struct.unpack(">H", raw[offset + 6 : offset + 8])
    seg_count = seg_count_x2 // 2

    end_code_off = offset + 14
    start_code_off = end_code_off + seg_count_x2 + 2  # +2 skips reservedPad
    id_delta_off = start_code_off + seg_count_x2
    id_range_offset_off = id_delta_off + seg_count_x2

    end_codes = struct.unpack(f">{seg_count}H", raw[end_code_off : end_code_off + seg_count_x2])
    start_codes = struct.unpack(
        f">{seg_count}H", raw[start_code_off : start_code_off + seg_count_x2]
    )
    id_deltas = struct.unpack(f">{seg_count}h", raw[id_delta_off : id_delta_off + seg_count_x2])
    id_range_offsets = struct.unpack(
        f">{seg_count}H", raw[id_range_offset_off : id_range_offset_off + seg_count_x2]
    )

    mapping: dict[int, int] = {}
    for index in range(seg_count):
        start, end = start_codes[index], end_codes[index]
        delta, range_offset = id_deltas[index], id_range_offsets[index]
        if start == 0xFFFF and end == 0xFFFF:
            continue
        for code in range(start, end + 1):
            if range_offset == 0:
                glyph_id = (code + delta) & 0xFFFF
            else:
                glyph_index_addr = (
                    id_range_offset_off + index * 2 + range_offset + (code - start) * 2
                )
                if glyph_index_addr + 2 > len(raw):
                    continue
                (raw_glyph_id,) = struct.unpack(">H", raw[glyph_index_addr : glyph_index_addr + 2])
                glyph_id = 0 if raw_glyph_id == 0 else (raw_glyph_id + delta) & 0xFFFF
            if glyph_id != 0:
                mapping[code] = glyph_id
    return mapping


def _parse_cmap_format12(raw: bytes, offset: int) -> dict[int, int]:
    (num_groups,) = struct.unpack(">I", raw[offset + 12 : offset + 16])
    mapping: dict[int, int] = {}
    position = offset + 16
    for _ in range(num_groups):
        start_char, end_char, start_glyph = struct.unpack(">III", raw[position : position + 12])
        for code in range(start_char, end_char + 1):
            mapping[code] = start_glyph + (code - start_char)
        position += 12
    return mapping


def _parse_os2(os2: bytes) -> tuple[int, bool, bool, bool, int | None]:
    if len(os2) < 6:
        raise TrueTypeError("this font's OS/2 table is too short to read a weight class from")
    (version,) = struct.unpack(">H", os2[0:2])
    (weight_class,) = struct.unpack(">H", os2[4:6])

    is_italic = is_bold = False
    if len(os2) >= 64:
        (fs_selection,) = struct.unpack(">H", os2[62:64])
        is_italic = bool(fs_selection & 0x0001)
        is_bold = bool(fs_selection & 0x0020)

    is_serif = False
    if len(os2) >= 32:
        (family_class,) = struct.unpack(">h", os2[30:32])
        is_serif = (family_class >> 8) in _SERIF_FAMILY_CLASSES

    cap_height: int | None = None
    if version >= 2 and len(os2) >= 90:
        (cap_height,) = struct.unpack(">h", os2[88:90])

    return weight_class, is_italic, is_bold, is_serif, cap_height


def _fixed_to_float(value: int) -> float:
    """A 16.16 fixed-point value (`post.italicAngle`'s own format) as a float."""
    if value & 0x80000000:
        value -= 1 << 32
    return value / 65536.0


# =============================================================================
# Subsetting
# =============================================================================


def _glyph_bytes(font: TrueTypeFont, glyph_id: int) -> bytes:
    start, end = font.loca[glyph_id], font.loca[glyph_id + 1]
    return font.glyf[start:end]


def _composite_component_offsets(glyph: bytes) -> list[int]:
    """Byte offsets, within `glyph`, of each component's `glyphIndex` field.

    Empty for a simple glyph (`numberOfContours >= 0`) or an empty one. Used
    both to find which glyphs a composite REFERENCES (`_closure`) and to patch
    those references to new glyph ids when the subset is built
    (`_build_subset`) - one piece of parsing, two uses, so they cannot drift.
    """
    if len(glyph) < 10:
        return []
    (number_of_contours,) = struct.unpack(">h", glyph[0:2])
    if number_of_contours >= 0:
        return []

    offsets: list[int] = []
    position = 10
    while position + 4 <= len(glyph):
        (flags,) = struct.unpack(">H", glyph[position : position + 2])
        offsets.append(position + 2)
        position += 4
        position += 4 if flags & _COMPOSITE_ARG_WORDS else 2
        if flags & _COMPOSITE_HAVE_SCALE:
            position += 2
        elif flags & _COMPOSITE_XY_SCALE:
            position += 4
        elif flags & _COMPOSITE_TWO_BY_TWO:
            position += 8
        if not flags & _COMPOSITE_MORE_COMPONENTS:
            break
    return offsets


def _closure(font: TrueTypeFont, glyph_ids: set[int]) -> set[int]:
    """`glyph_ids` plus every glyph reachable through a composite reference.

    An accented character is very often a composite of a base letter and a
    mark, referencing both by glyph id; embedding only the composite's own
    (empty) outline record would produce a document where that one character
    renders as nothing.
    """
    seen = set(glyph_ids)
    stack = list(glyph_ids)
    while stack:
        glyph = _glyph_bytes(font, stack.pop())
        for offset in _composite_component_offsets(glyph):
            (component_id,) = struct.unpack(">H", glyph[offset : offset + 2])
            if component_id not in seen:
                seen.add(component_id)
                stack.append(component_id)
    return seen


@dataclass(frozen=True, slots=True)
class _Subset:
    old_to_new: dict[int, int]
    glyf: bytes
    loca: tuple[int, ...]
    hmtx: tuple[tuple[int, int], ...]


def _build_subset(font: TrueTypeFont, glyph_ids: set[int]) -> _Subset:
    """Renumber `glyph_ids` (which must already be closed under
    `_closure`) to new sequential ids starting at 0, and build the
    `glyf`/`loca`/`hmtx` tables that hold exactly those glyphs.

    New glyph id == CID, by construction - see the module docstring on why
    `CIDToGIDMap` never needs to be more than `/Identity`.
    """
    ordered_old_ids = [0] + sorted(gid for gid in glyph_ids if gid != 0)
    old_to_new = {old: new for new, old in enumerate(ordered_old_ids)}

    glyf_parts: list[bytes] = []
    loca = [0]
    hmtx: list[tuple[int, int]] = []

    for old_id in ordered_old_ids:
        raw = bytearray(_glyph_bytes(font, old_id))
        for component_offset in _composite_component_offsets(bytes(raw)):
            (old_component,) = struct.unpack(">H", raw[component_offset : component_offset + 2])
            struct.pack_into(">H", raw, component_offset, old_to_new[old_component])
        if len(raw) % 2:
            raw.append(0)  # glyf records are conventionally padded to an even length
        glyf_parts.append(bytes(raw))
        loca.append(loca[-1] + len(raw))
        hmtx.append(font.hmtx[old_id])

    return _Subset(
        old_to_new=old_to_new, glyf=b"".join(glyf_parts), loca=tuple(loca), hmtx=tuple(hmtx)
    )


def _subset_head(font: TrueTypeFont) -> bytes:
    head = bytearray(font.head_bytes)
    struct.pack_into(">I", head, 8, 0)  # checkSumAdjustment - build_sfnt recomputes it
    struct.pack_into(">h", head, 50, 1)  # indexToLocFormat: long (uint32 offsets)
    return bytes(head)


def _subset_hhea(font: TrueTypeFont, subset: _Subset) -> bytes:
    hhea = bytearray(font.hhea_bytes)
    # Every subset glyph gets its own explicit (advance, lsb) pair in the new
    # hmtx table below, so numberOfHMetrics covers all of them - no glyph
    # shares another's advance width the way the ORIGINAL font's tail glyphs
    # might have.
    struct.pack_into(">H", hhea, 34, len(subset.hmtx))
    return bytes(hhea)


def _subset_maxp(font: TrueTypeFont, subset: _Subset) -> bytes:
    maxp = bytearray(font.maxp_bytes)
    struct.pack_into(">H", maxp, 4, len(subset.old_to_new))
    return bytes(maxp)


def _encode_hmtx(hmtx: tuple[tuple[int, int], ...]) -> bytes:
    return b"".join(struct.pack(">Hh", advance, lsb) for advance, lsb in hmtx)


def _encode_loca(loca: tuple[int, ...]) -> bytes:
    return b"".join(struct.pack(">I", offset) for offset in loca)


# =============================================================================
# sfnt assembly
# =============================================================================


def _checksum(data: bytes) -> int:
    padded = data + b"\x00" * (-len(data) % 4)
    total = 0
    for index in range(0, len(padded), 4):
        total = (total + int.from_bytes(padded[index : index + 4], "big")) & 0xFFFFFFFF
    return total


def build_sfnt(tables: dict[bytes, bytes]) -> bytes:
    """Assemble a TrueType-outline sfnt file from a table tag -> bytes map.

    The OpenType spec requires the table DIRECTORY to list tags in ascending
    order; the order tables are physically written in is free, and this uses
    the same sorted order for one fewer thing to track. If a `head` table is
    present, its `checkSumAdjustment` field (offset 8) is patched with the
    file's real checksum after everything else is laid out, per spec - the
    directory's own recorded checksum for `head` is computed with that field
    still zero, which is the specified (not mistaken) behaviour.
    """
    tags = sorted(tables)
    num_tables = len(tags)
    entry_selector = num_tables.bit_length() - 1 if num_tables else 0
    search_range = (1 << entry_selector) * 16
    range_shift = num_tables * 16 - search_range

    header = struct.pack(
        ">IHHHH", 0x00010000, num_tables, search_range, entry_selector, range_shift
    )

    table_start = 12 + 16 * num_tables
    directory = bytearray()
    body = bytearray()
    head_body_offset: int | None = None
    for tag in tags:
        data = tables[tag]
        if tag == b"head":
            head_body_offset = table_start + len(body)
        offset = table_start + len(body)
        directory += struct.pack(">4sIII", tag, _checksum(data), offset, len(data))
        body += data + b"\x00" * (-len(data) % 4)

    file_bytes = bytearray(header + bytes(directory) + bytes(body))

    if head_body_offset is not None:
        file_checksum = _checksum(bytes(file_bytes))
        adjustment = (0xB1B0AFBA - file_checksum) & 0xFFFFFFFF
        struct.pack_into(">I", file_bytes, head_body_offset + 8, adjustment)

    return bytes(file_bytes)


# =============================================================================
# PDF-facing embedding
# =============================================================================


def _subset_tag(payload: bytes) -> bytes:
    """A deterministic 6-uppercase-letter subset tag, per the `ABCDEF+Name`
    convention PDF readers use to signal "this is not the whole font".

    Convention elsewhere is a RANDOM tag; this codebase's rendering has to be
    byte-for-byte deterministic (FR-TPL-017, `api.invoicing.pdf`'s own
    docstring), so the tag is derived from the subset font's own bytes
    instead - the same input always produces the same tag, and two different
    subsets of the same font are still distinguishable.
    """
    digest = hashlib.sha256(payload).digest()
    return bytes(65 + (byte % 26) for byte in digest[:6])


@dataclass(frozen=True, slots=True)
class EmbeddedFont:
    """Everything `api.invoicing.pdf.render_pdf` needs to emit one
    `Type0`/`CIDFontType2` font, subset to exactly the text that uses it.

    Built by `build_embedded_font`, never constructed directly - the fields
    below are the PDF objects' own vocabulary (FontDescriptor entries, a `/W`
    width table, `Identity-H` code points), not a font format's.
    """

    #: The `/F3`-style PDF resource name a `Text.font` must equal to draw with
    #: this typeface instead of a base-14 face.
    resource_name: bytes
    base_font_name: bytes
    #: The subset sfnt program, for `/FontFile2`.
    font_program: bytes
    #: CID -> width, in 1000ths of an em (PDF's glyph-space convention,
    #: independent of the font's own `unitsPerEm`).
    widths: dict[int, int]
    default_width: int
    #: CID -> the code point it draws, for a CID-keyed ToUnicode CMap
    #: (`api.invoicing.pdfa.to_unicode_cmap_cid`).
    cid_to_unicode: dict[int, int]
    #: What a `Text` run drawn with this font must encode through - see
    #: `encode`.
    codepoint_to_cid: dict[int, int]
    flags: int
    font_bbox: tuple[int, int, int, int]
    italic_angle: float
    ascent: int
    descent: int
    cap_height: int
    stem_v: int

    def supports(self, value: str) -> bool:
        """Whether every character in `value` has a glyph in THIS subset.

        A caller decides, per text run, whether to draw with this font or
        fall back to a base-14 face - see `api.invoicing.rendering`'s
        honesty about typography it cannot apply.
        """
        return all(ord(char) in self.codepoint_to_cid for char in value)

    def encode(self, value: str) -> bytes:
        """`value` as 2-byte-per-glyph CIDs, big-endian - `Identity-H`'s show-
        text operand shape.
        """
        out = bytearray()
        for char in value:
            cid = self.codepoint_to_cid.get(ord(char))
            if cid is None:
                raise TrueTypeError(
                    f"{char!r} (U+{ord(char):04X}) is not among the code points "
                    f"this font was subset for. Call `supports()` before "
                    f"`encode()`, or subset for the text actually being drawn."
                )
            out += struct.pack(">H", cid)
        return bytes(out)


def build_embedded_font(
    font: TrueTypeFont, *, resource_name: bytes, base_font_name: bytes, codepoints: AbstractSet[int]
) -> EmbeddedFont:
    """Subset `font` to `codepoints` (closed over composite references) and
    package the result for `render_pdf`.

    `codepoints` should be exactly the Unicode code points a document's text
    draws in this typeface - not the font's whole Unicode coverage, and not a
    fixed "every character we might ever need" superset. That is what makes
    this genuine subsetting rather than embedding the whole font every time.
    """
    base_glyph_ids = {0}
    codepoint_to_glyph: dict[int, int] = {}
    for codepoint in codepoints:
        glyph_id = font.cmap.get(codepoint)
        if glyph_id is not None:
            base_glyph_ids.add(glyph_id)
            codepoint_to_glyph[codepoint] = glyph_id

    glyph_ids = _closure(font, base_glyph_ids)
    subset = _build_subset(font, glyph_ids)

    scale = 1000.0 / font.units_per_em
    widths = {
        subset.old_to_new[old_id]: round(font.hmtx[old_id][0] * scale) for old_id in glyph_ids
    }
    codepoint_to_cid = {
        codepoint: subset.old_to_new[glyph_id] for codepoint, glyph_id in codepoint_to_glyph.items()
    }
    # Ties (two code points sharing one glyph) resolve to whichever survives
    # this dict comprehension's iteration order - deterministic for a given
    # `codepoints` set (Python dicts preserve insertion order) but not a
    # claim that it is the "canonical" spelling. Rare in practice (case pairs
    # with identical outlines are the common example) and harmless: copying
    # the glyph out still produces A readable character, just not
    # necessarily the one originally typed.
    cid_to_unicode = {cid: codepoint for codepoint, cid in codepoint_to_cid.items()}

    cap_height = font.cap_height if font.cap_height is not None else round(font.ascender * 0.7)

    flags = 0
    if font.is_fixed_pitch:
        flags |= 1 << 0
    if font.is_serif:
        flags |= 1 << 1
    # Symbolic (bit 3): this embedding's encoding is Identity-H with no
    # claimed standard text encoding, which is exactly what PDF's Symbolic
    # flag describes - not a claim about the GLYPHS themselves being unusual.
    flags |= 1 << 2
    if font.is_italic:
        flags |= 1 << 6
    if font.is_bold or font.weight_class >= 600:
        flags |= 1 << 18  # ForceBold

    sfnt = build_sfnt(
        {
            b"head": _subset_head(font),
            b"hhea": _subset_hhea(font, subset),
            b"maxp": _subset_maxp(font, subset),
            b"hmtx": _encode_hmtx(subset.hmtx),
            b"loca": _encode_loca(subset.loca),
            b"glyf": subset.glyf,
        }
    )

    return EmbeddedFont(
        resource_name=resource_name,
        base_font_name=_subset_tag(sfnt) + b"+" + base_font_name,
        font_program=sfnt,
        widths=widths,
        default_width=round(font.hmtx[0][0] * scale) if font.hmtx else 0,
        cid_to_unicode=cid_to_unicode,
        codepoint_to_cid=codepoint_to_cid,
        flags=flags,
        font_bbox=(
            round(font.x_min * scale),
            round(font.y_min * scale),
            round(font.x_max * scale),
            round(font.y_max * scale),
        ),
        italic_angle=font.italic_angle,
        ascent=round(font.ascender * scale),
        descent=round(font.descender * scale),
        cap_height=round(cap_height * scale),
        # Industry-standard approximation (used by, among others, PDFBox and
        # iText) for a font format with no explicit stem-width field - see the
        # module docstring.
        stem_v=50 + round((font.weight_class / 65.0) ** 2),
    )
