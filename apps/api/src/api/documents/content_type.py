"""What a file actually is - SEC-005.

    SEC-005  File uploads: type verified by content not extension, size-capped,
             malware-scanned, stored outside the web root, served from a
             separate origin with `Content-Disposition: attachment`.

This module is the first clause. Nothing here ever looks at a filename.

--- Why the extension is not merely unreliable but irrelevant ---

An extension is a claim made by whoever uploaded the file, and the attack it
enables is not subtle: `invoice.pdf` holding HTML renders as a page if anything
ever serves it inline, and `receipt.jpg` holding an ELF binary is a payload
waiting for something downstream to execute it. The defences SEC-005 lists work
together - a separate origin and `Content-Disposition: attachment` mean a
mis-typed file is downloaded rather than rendered - but the cheapest place to
stop it is to decide the type from the bytes and store THAT.

So `sniff()` returns what the leading bytes say, and the caller records its
answer. `api.documents.service` never passes a filename into this decision, and
`document.content_type` in migration 0031 holds this function's output rather
than anything a client sent.

--- Why the accepted set is closed ---

FR-EXP-001 names what a person may capture: "JPEG, PNG, HEIC, PDF and
multi-page PDF". That is the list, and it is an allowlist rather than a
denylist because the denylist version of this decision has to be complete to be
correct and never is. A format nobody named is refused, including formats that
are perfectly ordinary elsewhere - SVG most of all, which is a script host
wearing an image's name and which FR-TPL-018 already treats as a special hazard.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class ContentTypeError(ValueError):
    """The bytes are not a document this product accepts."""


class DocumentContentType(enum.Enum):
    """The formats FR-EXP-001 names, and only those.

    The value is the media type recorded on the row and sent back on download.
    """

    JPEG = "image/jpeg"
    PNG = "image/png"
    HEIC = "image/heic"
    PDF = "application/pdf"


@dataclass(frozen=True, slots=True)
class Signature:
    content_type: DocumentContentType
    #: Bytes that must appear at `offset` for this format.
    magic: bytes
    offset: int = 0
    #: For ISO base media files, the brand that follows the `ftyp` box name.
    brands: tuple[bytes, ...] = ()


#: `ftyp` sits at offset 4; the four bytes before it are the box length.
_ISO_BMFF_MARKER = b"ftyp"
_ISO_BMFF_MARKER_OFFSET = 4

#: HEIC's brands. `mif1`/`msf1` are the generic HEIF brands that iOS also
#: produces, so accepting only `heic` would refuse photographs taken on the
#: devices FR-EXP-001's camera path is written for.
_HEIC_BRANDS = (
    b"heic",
    b"heix",
    b"heim",
    b"heis",
    b"hevc",
    b"hevx",
    b"hevm",
    b"hevs",
    b"mif1",
    b"msf1",
)

SIGNATURES: tuple[Signature, ...] = (
    # SOI marker plus the first byte of the next marker. Three bytes rather
    # than two, because `FF D8` alone matches far too much.
    Signature(DocumentContentType.JPEG, b"\xff\xd8\xff"),
    # The 8-byte PNG signature, which deliberately includes CRLF and EOF bytes
    # so that a transfer that mangled line endings is detectable.
    Signature(DocumentContentType.PNG, b"\x89PNG\r\n\x1a\n"),
    # `%PDF-`. The version digits that follow are not checked: a 1.4 and a 2.0
    # file are both PDFs, and pinning the version would refuse valid documents
    # for no security gain.
    Signature(DocumentContentType.PDF, b"%PDF-"),
    Signature(
        DocumentContentType.HEIC,
        _ISO_BMFF_MARKER,
        offset=_ISO_BMFF_MARKER_OFFSET,
        brands=_HEIC_BRANDS,
    ),
)

#: Enough bytes to reach the longest signature plus its brand. Callers may pass
#: the whole file; only this much is read.
SNIFF_LENGTH = 16

#: Formats that are refused with a specific reason rather than a generic one,
#: because each is a thing people genuinely try to upload and the difference
#: between "we do not take this" and "this looks like an attack" matters to
#: whoever reads the log.
_NAMED_REFUSALS: tuple[tuple[bytes, str], ...] = (
    (b"<?xml", "XML, and possibly SVG"),
    (b"<svg", "SVG"),
    (b"<!DOCTYPE", "HTML or XML"),
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
    end = signature.offset + len(signature.magic)
    if len(data) < end or data[signature.offset : end] != signature.magic:
        return False
    if not signature.brands:
        return True
    brand = data[end : end + 4]
    return brand in signature.brands


def sniff(data: bytes) -> DocumentContentType:
    """The format these bytes are, or a refusal.

    Takes bytes rather than a path or a stream on purpose: the decision must be
    made on the SAME bytes that get stored and hashed. Sniffing a stream and
    then storing whatever the stream yields afterwards is a
    time-of-check-to-time-of-use gap wide enough to drive a payload through.
    """
    if not data:
        raise ContentTypeError("the uploaded file is empty")

    for signature in SIGNATURES:
        if _matches(data, signature):
            return signature.content_type

    raise ContentTypeError(_refusal(data))


def _refusal(data: bytes) -> str:
    accepted = ", ".join(sorted(t.value for t in DocumentContentType))
    for magic, description in _NAMED_REFUSALS:
        if data.startswith(magic):
            return (
                f"this file is {description}, which is not a document type LEDGR "
                f"accepts (SEC-005). Accepted: {accepted}."
            )
    prefix = data[:8].hex(" ")
    return (
        f"the file's leading bytes ({prefix}) match no accepted document type "
        f"(SEC-005). Accepted: {accepted}. The filename is not consulted, so "
        f"renaming the file will not change this answer."
    )


def verify_declared(data: bytes, declared: str | None) -> DocumentContentType:
    """Sniff, and refuse a client whose own claim disagrees.

    The sniffed answer is authoritative either way - that is SEC-005's "by
    content not extension" - so the disagreement is not needed to decide the
    type. It is worth refusing anyway: a client that says `image/png` over a
    PDF is either broken or probing, and storing the file with a corrected
    type would resolve the contradiction silently in the attacker's favour.

    A client that declares nothing is fine. `application/octet-stream` counts
    as declaring nothing: it is what a browser sends when it has no opinion,
    and treating it as a contradiction would refuse ordinary uploads.
    """
    sniffed = sniff(data)
    if declared is None:
        return sniffed

    normalised = declared.split(";")[0].strip().lower()
    if normalised in ("", "application/octet-stream"):
        return sniffed

    if normalised != sniffed.value:
        raise ContentTypeError(
            f"the upload declares {normalised!r} and its bytes are "
            f"{sniffed.value!r}. The bytes decide (SEC-005), and a file that "
            f"disagrees with itself is refused rather than silently corrected."
        )
    return sniffed
