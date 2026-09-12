"""What a template asset actually is - SEC-005, FR-TPL-001, FR-TPL-018.

    SEC-005  File uploads: type verified by content not extension, size-capped,
             malware-scanned, stored outside the web root, served from a
             separate origin with `Content-Disposition: attachment`.

This is `api.documents.content_type`'s sibling, not its replacement. That
module's whole point is that SVG is refused outright - "a script host wearing
an image's name" - and it must keep refusing it: nothing here changes what
`api.documents` accepts. A LOGO is a narrower, deliberately different problem:
FR-TPL-001 explicitly asks for SVG among the accepted formats, and FR-TPL-018
exists precisely because that acceptance is dangerous by default. So this
module accepts PNG, JPEG **and** SVG, and the price of accepting SVG is that
every SVG sniffed here is required (by `api.templates.assets`, the caller) to
pass through `api.templates.svg_sanitizer.sanitize_svg` before it is stored or
rendered anywhere. Sniffing something as SVG is not a safety judgement; it is
only "route this to the sanitizer next."

--- Why SVG has no fixed byte signature ---

PNG and JPEG are binary formats with a fixed magic prefix - trivial to sniff
the way `api.documents.content_type.Signature` does. SVG is XML text, and a
conforming SVG file may legally begin with a byte-order mark, an XML
declaration, a comment, or a DOCTYPE before the root `<svg>` element ever
appears. There is no single fixed prefix to match. `sniff()` therefore looks
for the SHAPE of an SVG's opening - one of a small set of prefixes that only
ever start an XML/SVG document - rather than a single signature. This is
still "verified by content, not extension": the filename plays no part, and a
file whose bytes do not have this shape is refused regardless of what it is
named.

A DOCTYPE-laden or otherwise hostile SVG-shaped file is deliberately still
sniffed as SVG here rather than refused at this layer - `sanitize_svg` is
where the DOCTYPE/entity refusal belongs (see that module's docstring for
why), and duplicating a weaker version of that check here would only create
two places for the real gate to drift out of step.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class TemplateAssetContentTypeError(ValueError):
    """The bytes are not a template asset this product accepts."""


class TemplateAssetContentType(enum.Enum):
    """FR-TPL-001's three accepted logo formats, and only those. The value is
    the media type recorded on `template_asset.content_type` (migration 0042's
    CHECK constraint names exactly these three strings).
    """

    PNG = "image/png"
    JPEG = "image/jpeg"
    SVG = "image/svg+xml"


@dataclass(frozen=True, slots=True)
class Signature:
    content_type: TemplateAssetContentType
    magic: bytes


#: PNG and JPEG: the same fixed, binary signatures
#: `api.documents.content_type.SIGNATURES` uses, duplicated rather than
#: imported - the two modules accept different sets by design (this one adds
#: SVG, that one refuses it outright) and must never share a mutable list a
#: change to one could silently apply to the other.
_BINARY_SIGNATURES: tuple[Signature, ...] = (
    Signature(TemplateAssetContentType.JPEG, b"\xff\xd8\xff"),
    Signature(TemplateAssetContentType.PNG, b"\x89PNG\r\n\x1a\n"),
)

#: Enough bytes to reach the longest binary signature. SVG detection scans
#: further (see `_SVG_SCAN_WINDOW`), since a real-world SVG export routinely
#: carries an XML declaration and a comment before its root element.
SNIFF_LENGTH = 16

#: How far into the file to look for the shape of an SVG's opening. Generous
#: enough for an XML declaration, an encoding comment and a DOCTYPE with an
#: internal subset (which is exactly the case `sanitize_svg` most wants to see
#: and refuse) to all appear before `<svg`, while still bounded so sniffing
#: never reads the whole file into a decode attempt.
_SVG_SCAN_WINDOW = 4096

#: A UTF-8 byte-order mark, which a real editor or export tool sometimes
#: writes before the XML declaration. Stripped before any prefix check.
_UTF8_BOM = b"\xef\xbb\xbf"

#: Byte sequences that only ever open an XML or SVG document. Checked against
#: the START of the (BOM- and whitespace-stripped) content - not a substring
#: search - so a PNG whose compressed data happens to contain `<svg` bytes
#: deep inside it is never misclassified (PNG/JPEG are checked first anyway,
#: which already rules that out in practice).
_XML_OPENERS: tuple[bytes, ...] = (b"<?xml", b"<!--", b"<!doctype svg", b"<svg")

#: HTML's own DOCTYPE opening, checked so a plain HTML file gets a specific,
#: accurate refusal rather than being misread as "SVG-shaped" merely because
#: both start with `<!doctype`.
_HTML_DOCTYPE = b"<!doctype html"

#: Formats refused with a specific reason, mirroring
#: `api.documents.content_type._NAMED_REFUSALS`'s convention (duplicated, not
#: imported, for the same "these two lists must never silently share a
#: mutation" reason `_BINARY_SIGNATURES` gives). SVG and bare XML are absent
#: from this list on purpose: they are accepted here, not refused.
_NAMED_REFUSALS: tuple[tuple[bytes, str], ...] = (
    (b"<html", "HTML"),
    (b"PK\x03\x04", "a ZIP archive (or an Office file, which is one)"),
    (b"MZ", "a Windows executable"),
    (b"\x7fELF", "a Linux executable"),
    (b"\xca\xfe\xba\xbe", "a Java class or Mach-O binary"),
    (b"GIF8", "GIF"),
    (b"RIFF", "a RIFF container, such as WebP or WAV"),
    (b"BM", "a Windows bitmap"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
)


def _matches(data: bytes, signature: Signature) -> bool:
    magic = signature.magic
    return data[: len(magic)] == magic


def _stripped_prefix(data: bytes) -> bytes:
    """The leading bytes, with a UTF-8 BOM and ASCII whitespace removed, so a
    prefix check does not have to separately allow for either.
    """
    window = data[:_SVG_SCAN_WINDOW]
    if window.startswith(_UTF8_BOM):
        window = window[len(_UTF8_BOM) :]
    return window.lstrip(b" \t\r\n")


def sniff(data: bytes) -> TemplateAssetContentType:
    """The format these bytes are, or a refusal.

    Takes bytes, never a filename or a declared type - the same posture
    `api.documents.content_type.sniff` takes and for the same reason: the
    decision must be made on the SAME bytes that get sanitised (for SVG),
    scanned and stored.
    """
    if not data:
        raise TemplateAssetContentTypeError("the uploaded file is empty")

    for signature in _BINARY_SIGNATURES:
        if _matches(data, signature):
            return signature.content_type

    prefix = _stripped_prefix(data)
    lowered = prefix[:32].lower()

    if lowered.startswith(_HTML_DOCTYPE):
        raise TemplateAssetContentTypeError(_refusal(data, "HTML"))
    if any(lowered.startswith(opener) for opener in _XML_OPENERS):
        return TemplateAssetContentType.SVG

    raise TemplateAssetContentTypeError(_refusal(data))


def _refusal(data: bytes, description: str | None = None) -> str:
    accepted = ", ".join(sorted(t.value for t in TemplateAssetContentType))
    if description is None:
        for magic, candidate in _NAMED_REFUSALS:
            if data.startswith(magic):
                description = candidate
                break
    if description is not None:
        return (
            f"this file is {description}, which is not a logo type LEDGR accepts "
            f"(FR-TPL-001, SEC-005). Accepted: {accepted}."
        )
    prefix = data[:8].hex(" ")
    return (
        f"the file's leading bytes ({prefix}) match no accepted logo type "
        f"(FR-TPL-001, SEC-005). Accepted: {accepted}. The filename is not "
        f"consulted, so renaming the file will not change this answer."
    )
