"""SEC-005's first clause: type verified by content, not extension.

The point of these tests is not that a PNG is recognised. It is that a file
named `receipt.pdf` holding something else is refused no matter what it claims,
because the filename never enters the decision at all.
"""

from __future__ import annotations

import pytest

from api.documents.content_type import (
    SNIFF_LENGTH,
    ContentTypeError,
    DocumentContentType,
    sniff,
    verify_declared,
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 32
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"\x00" * 32

ACCEPTED = [
    pytest.param(JPEG, DocumentContentType.JPEG, id="jpeg"),
    pytest.param(PNG, DocumentContentType.PNG, id="png"),
    pytest.param(PDF, DocumentContentType.PDF, id="pdf"),
    pytest.param(HEIC, DocumentContentType.HEIC, id="heic"),
]


@pytest.mark.parametrize(("data", "expected"), ACCEPTED)
def test_the_formats_fr_exp_001_names_are_accepted(
    data: bytes, expected: DocumentContentType
) -> None:
    """FR-EXP-001: "JPEG, PNG, HEIC, PDF and multi-page PDF"."""
    assert sniff(data) is expected


@pytest.mark.parametrize(
    "brand",
    [b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"],
    ids=lambda b: b.decode(),
)
def test_every_heif_brand_an_iphone_produces_is_accepted(brand: bytes) -> None:
    """Accepting only the literal `heic` brand would refuse photographs taken
    on the devices FR-EXP-001's camera path is written for: iOS writes `mif1`
    and `msf1` too.
    """
    assert sniff(b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 16) is DocumentContentType.HEIC


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "SVG is a script host"),
        (b"<!DOCTYPE html><html><body>hello", "HTML renders"),
        (b"<?xml version='1.0'?><svg/>", "XML, and possibly SVG"),
        (b"PK\x03\x04\x14\x00\x00\x00", "a ZIP, which an Office file is"),
        (b"MZ\x90\x00\x03\x00\x00\x00", "a Windows executable"),
        (b"\x7fELF\x02\x01\x01\x00", "a Linux executable"),
        (b"GIF89a\x01\x00", "GIF is not on the list"),
        (b"RIFF\x00\x00\x00\x00WEBP", "WebP is not on the list"),
        (b"II*\x00\x08\x00\x00\x00", "TIFF is not on the list"),
        (b"BM\x36\x00\x00\x00", "BMP is not on the list"),
    ],
    ids=lambda v: v if isinstance(v, str) else "bytes",
)
def test_everything_else_is_refused(data: bytes, why: str) -> None:
    with pytest.raises(ContentTypeError):
        sniff(data)


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(ContentTypeError, match="empty"):
        sniff(b"")


def test_a_file_shorter_than_its_signature_is_refused() -> None:
    """`%PD` is a prefix of `%PDF-` and is not a PDF. A length check that
    compared only what was present would accept it.
    """
    with pytest.raises(ContentTypeError):
        sniff(b"%PD")
    with pytest.raises(ContentTypeError):
        sniff(b"\x89PNG")


def test_the_filename_is_never_consulted() -> None:
    """SEC-005 in one assertion, and the reason this module takes bytes rather
    than a path: there is no parameter a filename could arrive through.
    """
    import inspect

    parameters = inspect.signature(sniff).parameters
    assert list(parameters) == ["data"]
    assert parameters["data"].annotation in (bytes, "bytes")


def test_a_polyglot_is_judged_by_its_leading_bytes() -> None:
    """A file that begins as a PDF and contains HTML later is a PDF, and is
    served as one - with `Content-Disposition: attachment` and `nosniff`, which
    is what stops the HTML mattering. What must NOT happen is the reverse: HTML
    first with a PDF header buried inside is not a PDF.
    """
    assert sniff(PDF + b"<html><script>alert(1)</script></html>") is DocumentContentType.PDF

    with pytest.raises(ContentTypeError):
        sniff(b"<html><script>alert(1)</script></html>" + PDF)


def test_only_the_leading_bytes_are_needed() -> None:
    """The service hands over the whole file; a caller that reads a prefix gets
    the same answer, which is what makes an early rejection possible.
    """
    assert sniff(PDF[:SNIFF_LENGTH]) is DocumentContentType.PDF


class TestDeclaredType:
    def test_a_client_that_declares_nothing_is_fine(self) -> None:
        assert verify_declared(PNG, None) is DocumentContentType.PNG

    @pytest.mark.parametrize(
        "declared", ["application/octet-stream", "", "   ", "APPLICATION/OCTET-STREAM"]
    )
    def test_octet_stream_counts_as_declaring_nothing(self, declared: str) -> None:
        """It is what a browser sends when it has no opinion. Treating it as a
        contradiction would refuse ordinary uploads.
        """
        assert verify_declared(PNG, declared) is DocumentContentType.PNG

    def test_a_matching_declaration_passes(self) -> None:
        assert verify_declared(PDF, "application/pdf") is DocumentContentType.PDF
        # Parameters are ignored: `; charset=` is noise on a binary type but
        # clients send it.
        assert verify_declared(PNG, "image/png; charset=binary") is DocumentContentType.PNG
        assert verify_declared(PNG, "IMAGE/PNG") is DocumentContentType.PNG

    def test_a_contradiction_is_refused_rather_than_corrected(self) -> None:
        """The sniffed answer is authoritative either way, so this refusal is
        not needed to decide the type. It is worth making anyway: a client that
        says `image/png` over a PDF is broken or probing, and silently storing
        the corrected type would resolve the contradiction in its favour.
        """
        with pytest.raises(ContentTypeError, match="disagrees with itself"):
            verify_declared(PDF, "image/png")

    def test_the_bytes_win_over_a_declaration_that_names_a_rejected_type(self) -> None:
        # Declaring `image/svg+xml` over PNG bytes is still a contradiction,
        # not an SVG upload.
        with pytest.raises(ContentTypeError):
            verify_declared(PNG, "image/svg+xml")
