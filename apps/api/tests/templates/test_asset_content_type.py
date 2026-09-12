"""FR-TPL-001, SEC-005: template assets are typed by content, never by
filename or declared type - and unlike `api.documents.content_type`, SVG is
one of the accepted types here.
"""

from __future__ import annotations

import pytest

from api.templates.asset_content_type import (
    TemplateAssetContentType,
    TemplateAssetContentTypeError,
    sniff,
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
BARE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
XML_DECL_SVG = b'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>'
COMMENT_SVG = b'<!-- Generator: Adobe Illustrator -->\n<svg xmlns="http://www.w3.org/2000/svg"/>'
DOCTYPE_SVG = (
    b'<?xml version="1.0"?>\n<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
    b'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
    b'<svg xmlns="http://www.w3.org/2000/svg"/>'
)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(JPEG, TemplateAssetContentType.JPEG, id="jpeg"),
        pytest.param(PNG, TemplateAssetContentType.PNG, id="png"),
        pytest.param(BARE_SVG, TemplateAssetContentType.SVG, id="bare-svg"),
        pytest.param(XML_DECL_SVG, TemplateAssetContentType.SVG, id="svg-with-xml-decl"),
        pytest.param(COMMENT_SVG, TemplateAssetContentType.SVG, id="svg-with-leading-comment"),
        pytest.param(DOCTYPE_SVG, TemplateAssetContentType.SVG, id="svg-with-doctype"),
    ],
)
def test_the_three_fr_tpl_001_formats_are_accepted(
    data: bytes, expected: TemplateAssetContentType
) -> None:
    assert sniff(data) is expected


def test_leading_whitespace_and_a_bom_do_not_defeat_svg_detection() -> None:
    data = b"\xef\xbb\xbf   \n" + BARE_SVG
    assert sniff(data) is TemplateAssetContentType.SVG


@pytest.mark.parametrize(
    "data",
    [
        b"<!DOCTYPE html><html><body>hello</body></html>",
        b"PK\x03\x04\x14\x00\x00\x00",
        b"MZ\x90\x00\x03\x00\x00\x00",
        b"\x7fELF\x02\x01\x01\x00",
        b"GIF89a\x01\x00",
        b"RIFF\x00\x00\x00\x00WEBP",
        b"II*\x00\x08\x00\x00\x00",
        b"BM\x36\x00\x00\x00",
    ],
    ids=["html", "zip", "exe", "elf", "gif", "webp", "tiff", "bmp"],
)
def test_everything_else_is_refused(data: bytes) -> None:
    with pytest.raises(TemplateAssetContentTypeError):
        sniff(data)


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(TemplateAssetContentTypeError, match="empty"):
        sniff(b"")


def test_the_filename_is_never_consulted() -> None:
    import inspect

    parameters = inspect.signature(sniff).parameters
    assert list(parameters) == ["data"]
    assert parameters["data"].annotation in (bytes, "bytes")
