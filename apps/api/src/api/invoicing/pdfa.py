"""What makes a PDF archival - FR-TPL-015, and the half of FR-TPL-016 that is
about text rather than structure.

`api.invoicing.pdf` builds a PDF. This module holds the pieces that make one
claim to be PDF/A, and - more importantly - the gate that stops it claiming so
when it is not.

--- The central decision: the file may not lie about itself ---

PDF/A conformance is asserted INSIDE the file, in an XMP packet:

    <pdfaid:part>3</pdfaid:part>
    <pdfaid:conformance>A</pdfaid:conformance>

Nothing checks that on the way out. A file can carry that claim and fail every
validator, and the failure surfaces years later - when somebody tries to rely on
an archive they were told was archival. For a document CMP-001 keeps for seven
years and an inspector may ask for, a false claim is materially worse than an
honest absence: an unmarked PDF is obviously just a PDF, while a marked one that
fails veraPDF is a document somebody stopped checking.

So `assert_conformance` refuses to emit the claim unless every requirement it
can verify is met, and `render_pdf` raises rather than downgrading silently.
The requirements are listed as data (`REQUIREMENTS`) so the gap between what is
built and what is claimed is readable rather than folkloric.

--- Where we actually are ---

Everything PDF/A-3 needs is either built or seamed EXCEPT font embedding, and
that one is not a coding gap: it needs a licensed font binary this repository
does not contain (FR-TPL-002 calls for "licensed, PDF-embeddable" faces, and
FR-TPL-020 rules out user-uploaded ones). PDF/A forbids relying on the base-14
fonts a reader happens to have, which is exactly what this writer does today.

So `Conformance.PDF_A_3A` raises with that one gap named, and will start
succeeding the moment a font is embedded - no other change. The tagging,
Unicode mapping, metadata, identifier and output-intent machinery are all here
and all tested, because they are what the font work would otherwise be
blocked behind.

--- Why the ICC profile is supplied rather than generated ---

PDF/A requires an OutputIntent carrying an embedded ICC colour profile. Writing
one by hand is possible and is a bad idea: an ICC profile is a binary format
whose correctness cannot be checked by reading it, and a subtly wrong one
produces documents that validate structurally and render with shifted colour.
So the bytes are a parameter. A deployment that has an sRGB profile passes it;
one that does not gets a named gap rather than a fabricated profile - the same
posture `api.customers.peppol` takes about inventing a participant id.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = [
    "Conformance",
    "Requirement",
    "ConformanceGap",
    "REQUIREMENTS",
    "conformance_gaps",
    "assert_conformance",
    "xmp_packet",
    "to_unicode_cmap",
    "to_unicode_cmap_cid",
    "output_intent",
]


class Conformance(enum.Enum):
    """What a rendered file claims to be.

    `NONE` is an ordinary PDF - still tagged, still Unicode-mapped, because
    FR-TPL-016 asks for those independently of archival conformance. It simply
    makes no claim about ISO 19005.
    """

    NONE = "none"
    #: ISO 19005-3 level B: visually reproducible for ever. No structure
    #: requirement.
    PDF_A_3B = "pdfa-3b"
    #: Level A: everything in B, plus tagged structure and Unicode mapping -
    #: which is what FR-TPL-016 asks for, so it is the target that matters here.
    PDF_A_3A = "pdfa-3a"

    @property
    def is_archival(self) -> bool:
        return self is not Conformance.NONE

    @property
    def part(self) -> int:
        return 3

    @property
    def level(self) -> str:
        return "A" if self is Conformance.PDF_A_3A else "B"


@dataclass(frozen=True, slots=True)
class Requirement:
    """One thing ISO 19005-3 asks for, and what satisfies it here."""

    #: The clause, so somebody checking against the standard can find it.
    clause: str
    summary: str
    #: True where this writer satisfies it today.
    satisfied: bool
    #: What would close it, where it is not satisfied. Written for whoever picks
    #: the work up, not for a user.
    closes_with: str = ""


#: Every PDF/A-3 requirement this writer is in a position to affect, and where
#: it stands. Listed as data rather than prose so `conformance_gaps` can be
#: derived from it and the two cannot drift.
#:
#: Requirements NOT listed are ones the writer satisfies by construction and
#: could not violate if it tried: no encryption (nothing encrypts), no
#: JavaScript (nothing writes an action), no external content references
#: (every stream is embedded), no LZW, no transparency groups.
REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        clause="6.3.4",
        summary="every font is embedded",
        satisfied=False,
        closes_with=(
            "a licensed TrueType or OpenType face embedded as a font file, with a "
            "FontDescriptor and a subset. The base-14 fonts this writer uses are "
            "supplied by the reader, which PDF/A forbids precisely because the "
            "reader in ten years may not have them. Needs a font binary and a "
            "licence (FR-TPL-002), not more code here."
        ),
    ),
    Requirement(
        clause="6.2.2",
        summary="an OutputIntent with an embedded ICC profile",
        satisfied=False,
        closes_with=(
            "sRGB ICC profile bytes passed to render_pdf. Supplied rather than "
            "generated - see this module's docstring."
        ),
    ),
    Requirement(
        clause="6.7.2",
        summary="XMP metadata identifying the conformance level",
        satisfied=True,
    ),
    Requirement(
        clause="6.7.3",
        summary="document information consistent with the XMP",
        satisfied=True,
    ),
    Requirement(
        clause="6.1.3",
        summary="a file identifier in the trailer",
        satisfied=True,
    ),
    Requirement(
        clause="6.8",
        summary="tagged structure with a role map (level A only)",
        satisfied=True,
    ),
    Requirement(
        clause="6.3.8",
        summary="a ToUnicode CMap so text extracts correctly (level A only)",
        satisfied=True,
    ),
    Requirement(
        clause="6.8.3",
        summary="alternative descriptions on figures (level A only)",
        satisfied=True,
    ),
    Requirement(
        clause="6.9",
        summary="a natural language declared for the document",
        satisfied=True,
    ),
)


@dataclass(frozen=True, slots=True)
class ConformanceGap:
    clause: str
    summary: str
    closes_with: str

    def __str__(self) -> str:
        return f"ISO 19005-3 {self.clause} ({self.summary}): {self.closes_with}"


class ConformanceNotMet(ValueError):
    """The file was asked to claim a conformance it does not have.

    Raised rather than downgraded. A caller who asked for PDF/A got a plain PDF
    silently would archive it believing otherwise, which is the failure this
    whole module exists to prevent.
    """

    def __init__(self, target: Conformance, gaps: tuple[ConformanceGap, ...]) -> None:
        self.target = target
        self.gaps = gaps
        listed = "\n".join(f"  - {gap}" for gap in gaps)
        super().__init__(
            f"this document cannot claim {target.value} and will not pretend to "
            f"(FR-TPL-015). Outstanding:\n{listed}"
        )


def conformance_gaps(
    target: Conformance,
    *,
    fonts_embedded: bool,
    icc_profile: bytes | None,
    unembedded_fonts: frozenset[str] = frozenset(),
) -> tuple[ConformanceGap, ...]:
    """What stands between this document and `target`.

    `fonts_embedded`/`icc_profile` are the two requirements a caller can
    actually change; everything else in `REQUIREMENTS` is a property of this
    writer. Level B does not require tagging or Unicode mapping, so those
    clauses are dropped from its list - but they are satisfied anyway, which
    is why the level A target is the interesting one.

    `unembedded_fonts` names which fonts are the problem when
    `fonts_embedded` is False - `api.invoicing.pdf.render_pdf` passes the
    document's own non-embedded font labels (never empty when
    `fonts_embedded` is False and any text was drawn at all), so the 6.3.4 gap
    says WHICH text is still relying on a reader-supplied face rather than
    only that some is - the specific D5 detail a mixed embedded-heading/
    base-14-body document needs to be actionable.
    """
    if not target.is_archival:
        return ()

    level_a_only = {"6.8", "6.3.8", "6.8.3"}
    gaps: list[ConformanceGap] = []

    for requirement in REQUIREMENTS:
        if target is Conformance.PDF_A_3B and requirement.clause in level_a_only:
            continue

        satisfied = requirement.satisfied
        closes_with = requirement.closes_with
        if requirement.clause == "6.3.4":
            satisfied = fonts_embedded
            if not satisfied and unembedded_fonts:
                closes_with = (
                    f"{closes_with} Still drawing text with a non-embedded font: "
                    f"{', '.join(sorted(unembedded_fonts))}."
                )
        elif requirement.clause == "6.2.2":
            satisfied = icc_profile is not None

        if not satisfied:
            gaps.append(
                ConformanceGap(
                    clause=requirement.clause, summary=requirement.summary, closes_with=closes_with
                )
            )

    return tuple(gaps)


def assert_conformance(
    target: Conformance,
    *,
    fonts_embedded: bool,
    icc_profile: bytes | None,
    unembedded_fonts: frozenset[str] = frozenset(),
) -> None:
    """Refuse to emit a claim this file cannot support."""
    gaps = conformance_gaps(
        target,
        fonts_embedded=fonts_embedded,
        icc_profile=icc_profile,
        unembedded_fonts=unembedded_fonts,
    )
    if gaps:
        raise ConformanceNotMet(target, gaps)


# --- XMP ---------------------------------------------------------------------

#: The magic identifier every XMP packet carries. Fixed by the specification;
#: it is not a value anybody chose.
_XPACKET_ID = "W5M0MpCehiHzreSzNTczkc9d"


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def xmp_packet(*, title: str, language: str, conformance: Conformance) -> bytes:
    """The metadata stream, as PDF/A requires it.

    Carries no `xmp:CreateDate` or `xmp:ModifyDate`. Both are conventional and
    both would be the only non-deterministic bytes in the document - and
    determinism is what makes the archive's content hash a property of the
    INVOICE rather than of the moment it was rendered (see `api.invoicing.pdf`).
    PDF/A does not require either.

    The `pdfaid` block appears only for an archival target, so a plain PDF makes
    no claim at all rather than a weak one.
    """
    identification = ""
    if conformance.is_archival:
        identification = f"""
  <rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">
   <pdfaid:part>{conformance.part}</pdfaid:part>
   <pdfaid:conformance>{conformance.level}</pdfaid:conformance>
  </rdf:Description>"""

    packet = f"""<?xpacket begin="﻿" id="{_XPACKET_ID}"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{_xml_escape(title)}</rdf:li></rdf:Alt>
   </dc:title>
   <dc:language><rdf:Bag><rdf:li>{_xml_escape(language)}</rdf:li></rdf:Bag></dc:language>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:pdf="http://ns.adobe.com/pdf/1.3/">
   <pdf:Producer>LEDGR</pdf:Producer>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">
   <xmp:CreatorTool>LEDGR</xmp:CreatorTool>
  </rdf:Description>{identification}
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""
    return packet.encode("utf-8")


# --- ToUnicode ---------------------------------------------------------------


def to_unicode_cmap(mapping: dict[int, int]) -> bytes:
    """The CMap that makes text SELECTABLE, per FR-TPL-016.

    `mapping` is byte code -> Unicode code point, and is `pdf.py`'s to build:
    this module has no opinion on WinAnsi, Latin-1 or any other encoding, and
    taking the table as a parameter rather than importing it is what keeps that
    true. The alternative - reaching into `api.invoicing.pdf` for its private
    encoding table - would make this module depend on the one it is imported
    BY, which is the reverse of every other dependency in this package.

    Without a ToUnicode CMap a reader shows the glyphs correctly and copies out
    nothing usable: the euro sign becomes a control character, and every
    accented letter in a Dutch company name extracts as something else. It is
    also what a screen reader reads, so this is half of FR-TPL-016 and the half
    that is invisible until somebody tries to use it.

    `bfchar` entries are emitted in code order, which keeps the output
    deterministic.
    """
    entries = sorted(mapping.items())

    blocks: list[bytes] = []
    # The format permits at most 100 entries per `bfchar` block.
    for start in range(0, len(entries), 100):
        chunk = entries[start : start + 100]
        lines = b"".join(b"<%02X> <%04X>\n" % (code, char) for code, char in chunk)
        blocks.append(b"%d beginbfchar\n%sendbfchar\n" % (len(chunk), lines))

    return (
        b"/CIDInit /ProcSet findresource begin\n"
        b"12 dict begin\nbegincmap\n"
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        + b"".join(blocks)
        + b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    )


def to_unicode_cmap_cid(mapping: dict[int, int]) -> bytes:
    """`to_unicode_cmap`'s CID-keyed sibling, for a `Type0`/`Identity-H` font.

    `mapping` is CID -> Unicode code point (`api.invoicing.truetype.
    EmbeddedFont.cid_to_unicode`). `Identity-H`'s character codes are TWO
    BYTES - a CID directly - so the codespace and every `bfchar` entry use
    4 hex digits here, where `to_unicode_cmap`'s single-byte WinAnsi codes use
    2. Reusing that function's `<%02X>` shape for a CID would silently
    mis-map every code above 0xFF: exactly the "common source of real bugs"
    a CID-keyed ToUnicode CMap is warned about elsewhere in this codebase.
    """
    entries = sorted(mapping.items())

    blocks: list[bytes] = []
    for start in range(0, len(entries), 100):
        chunk = entries[start : start + 100]
        lines = b"".join(b"<%04X> <%04X>\n" % (code, char) for code, char in chunk)
        blocks.append(b"%d beginbfchar\n%sendbfchar\n" % (len(chunk), lines))

    return (
        b"/CIDInit /ProcSet findresource begin\n"
        b"12 dict begin\nbegincmap\n"
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        + b"".join(blocks)
        + b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    )


# --- OutputIntent ------------------------------------------------------------


def output_intent(*, icc_object_number: int, components: int = 3) -> bytes:
    """The `/OutputIntent` dictionary naming the embedded profile.

    `sRGB IEC61966-2.1` is named as the condition because that is what the
    profile a deployment supplies is expected to be. The identifier is what a
    validator reports, so it has to describe the bytes actually embedded rather
    than an aspiration.
    """
    return (
        b"<< /Type /OutputIntent /S /GTS_PDFA1 "
        b"/OutputConditionIdentifier (sRGB IEC61966-2.1) "
        b"/Info (sRGB IEC61966-2.1) "
        b"/RegistryName (http://www.color.org) "
        b"/DestOutputProfile %d 0 R >>" % icc_object_number
    )
