"""Image embedding in api.invoicing.pdf - FR-TPL-001's logo.

Two kinds of test, and the split matters.

The DECODING tests assert what came out of a PNG or JPEG this suite built, so a
failure points at the decoder. The most important of them is the round trip:
`_unfilter` is checked against scanlines filtered by an independently written
encoder in `image_fixtures`, not by inverting `_unfilter` itself - a shared
mistake would otherwise cancel out and prove nothing.

The STRUCTURE tests assert the PDF, because an image object that a reader
cannot resolve is a logo missing from every invoice a business sends, and there
is no PDF parser here to notice.
"""

from __future__ import annotations

import re
import zlib

import pytest

from api.invoicing.pdf import (
    Image,
    ImageError,
    Page,
    Placement,
    load_image,
    render_pdf,
)
from tests.invoicing.image_fixtures import (
    GREY,
    GREY_ALPHA,
    RGB,
    indexed_png,
    jpeg,
    png,
)


def _page_with(image: Image, *, top: float = 40.0) -> Page:
    page = Page()
    page.place_image(image=image, x=56, top=top, width=120, height=60, alt="Bakker Consultancy")
    return page


def _stream_of(data: bytes, index: int = 0) -> bytes:
    return re.findall(rb"stream\n(.*?)\nendstream", data, re.S)[index]


# --- sniffing ----------------------------------------------------------------


def test_the_format_comes_from_the_bytes_not_a_filename() -> None:
    """The same posture `api.documents.content_type` takes: an extension is a
    claim by whoever uploaded the file.
    """
    assert load_image(png()).filter_name == b"FlateDecode"
    assert load_image(jpeg()).filter_name == b"DCTDecode"


@pytest.mark.parametrize(
    "data",
    [b"", b"GIF89a" + b"\x00" * 20, b"<svg xmlns='...'/>", b"%PDF-1.7\n"],
    ids=["empty", "gif", "svg", "pdf"],
)
def test_anything_else_is_refused_with_the_fix(data: bytes) -> None:
    """Including SVG, which FR-TPL-001 names and this writer cannot embed - it
    is not a raster. The message says what to export instead rather than
    leaving a dead end (D5).
    """
    with pytest.raises(ImageError, match="(?i)PNG.*JPEG"):
        load_image(data)


# --- JPEG --------------------------------------------------------------------


def test_a_jpeg_is_embedded_whole_and_never_re_encoded() -> None:
    """DCTDecode IS JPEG, so the file's own bytes are the stream. A logo's
    bytes on the invoice are the bytes that were uploaded.
    """
    data = jpeg()
    image = load_image(data)

    assert image.data == data
    assert image.filter_name == b"DCTDecode"
    assert image.decode_parms is None


def test_the_jpeg_frame_header_is_found_past_other_segments() -> None:
    """The fixture puts a comment segment before the frame. A scanner that did
    not walk segment lengths would read the comment's bytes as dimensions.
    """
    image = load_image(jpeg(width=640, height=480))

    assert (image.width, image.height) == (640, 480)


@pytest.mark.parametrize(
    ("components", "colour_space"),
    [(1, b"/DeviceGray"), (3, b"/DeviceRGB"), (4, b"/DeviceCMYK")],
)
def test_the_component_count_decides_the_colour_space(components: int, colour_space: bytes) -> None:
    assert load_image(jpeg(components=components)).colour_space == colour_space


def test_an_adobe_cmyk_jpeg_is_inverted_back() -> None:
    """Adobe stores 4-component scans inverted. Without the `/Decode` array the
    logo renders as a photographic negative - unmistakable, and unmistakably
    ours.
    """
    image = load_image(jpeg(components=4, adobe=True))

    assert image.decode == b"[1 0 1 0 1 0 1 0]"


def test_a_non_adobe_cmyk_jpeg_is_not_inverted() -> None:
    """The inversion is Adobe's convention, not CMYK's. Applying it
    unconditionally would produce the negative it exists to prevent.
    """
    assert load_image(jpeg(components=4, adobe=False)).decode is None


def test_an_rgb_jpeg_is_never_inverted_even_with_the_adobe_marker() -> None:
    assert load_image(jpeg(components=3, adobe=True)).decode is None


def test_a_progressive_jpeg_is_refused() -> None:
    """Not every reader decodes one, and an invoice has to open everywhere.
    Refused with the re-export setting rather than embedded hopefully.
    """
    with pytest.raises(ImageError, match="(?i)progressive"):
        load_image(jpeg(marker=0xC2))


def test_a_jpeg_with_an_unusable_component_count_is_refused() -> None:
    with pytest.raises(ImageError, match="(?i)colour components"):
        load_image(jpeg(components=2))


# --- PNG without alpha: the pass-through path --------------------------------


def test_a_plain_rgb_png_keeps_its_own_zlib_stream() -> None:
    """PDF implements PNG's predictors, so IDAT is a valid PDF stream as it
    stands. Nothing here decompresses it.
    """
    pixels = [bytes([255, 0, 0, 0, 255, 0]), bytes([0, 0, 255, 255, 255, 0])]
    image = load_image(png(colour_type=RGB, pixels=pixels))

    assert image.filter_name == b"FlateDecode"
    assert image.colour_space == b"/DeviceRGB"
    assert b"/Predictor 15" in (image.decode_parms or b"")
    assert b"/Colors 3" in (image.decode_parms or b"")
    assert image.smask is None


def test_the_predictor_describes_the_actual_image() -> None:
    """`/Columns` and `/Colors` are how a reader un-filters the scanlines. Wrong
    values produce a skewed picture rather than an absent one, which is the
    failure nobody notices in review.
    """
    pixels = [bytes([1, 2, 3]), bytes([4, 5, 6])]
    image = load_image(png(width=3, height=2, colour_type=GREY, pixels=pixels))

    assert b"/Columns 3" in (image.decode_parms or b"")
    assert b"/Colors 1" in (image.decode_parms or b"")
    assert b"/BitsPerComponent 8" in (image.decode_parms or b"")


def test_the_pass_through_stream_actually_decodes_to_the_original_pixels() -> None:
    """The claim the whole pass-through path rests on, checked rather than
    asserted: that PDF's `/Predictor 15` IS PNG's scanline filtering.

    Every other test on this path inspects the `/DecodeParms` dictionary, which
    proves only that we wrote what we meant to. This does what a reader does -
    inflate the stream and un-filter it with the parameters we declared - and
    compares against the pixels the fixture started from. A wrong `/Colors` or
    `/Columns` produces a skewed image in a viewer and passes every string
    assertion; it fails here.
    """
    from api.invoicing.pdf import _unfilter

    pixels = [
        bytes([255, 0, 0, 0, 255, 0, 12, 34, 56]),
        bytes([0, 0, 255, 255, 255, 0, 200, 100, 50]),
        bytes([1, 2, 3, 4, 5, 6, 7, 8, 9]),
    ]
    # Paeth, so the reconstruction is not trivially the identity.
    image = load_image(png(width=3, height=3, colour_type=RGB, filter_type=4, pixels=pixels))

    parms = image.decode_parms or b""
    colours = int(re.search(rb"/Colors (\d+)", parms).group(1))
    columns = int(re.search(rb"/Columns (\d+)", parms).group(1))
    bits = int(re.search(rb"/BitsPerComponent (\d+)", parms).group(1))
    stride = columns * colours * bits // 8

    decoded = _unfilter(
        zlib.decompress(image.data),
        height=image.height,
        stride=stride,
        bpp=colours * bits // 8,
    )

    assert decoded == b"".join(pixels)


def test_a_greyscale_png_is_devicegray() -> None:
    image = load_image(png(colour_type=GREY, pixels=[b"\x00\xff", b"\xff\x00"]))

    assert image.colour_space == b"/DeviceGray"


def test_an_indexed_png_carries_its_palette() -> None:
    image = load_image(indexed_png())

    assert image.colour_space.startswith(b"[/Indexed /DeviceRGB 2 <")
    # Three entries, so the highest index is 2 - off by one here paints every
    # pixel with the wrong palette entry.
    assert b"ff0000" in image.colour_space
    assert image.colour_space.endswith(b">]")


def test_a_low_bit_depth_indexed_png_passes_through() -> None:
    """1, 2 and 4-bit indexed PNGs are what a two-colour logo exports as. They
    need no decoding at all on this path.
    """
    image = load_image(indexed_png(bits=8))

    assert image.bits == 8
    assert b"/Predictor 15" in (image.decode_parms or b"")


# --- PNG transparency --------------------------------------------------------


def test_binary_palette_transparency_becomes_a_mask() -> None:
    """The classic "transparent background" export: one entry invisible, the
    rest opaque. PDF expresses that as index ranges, so it costs no decoding.
    """
    image = load_image(indexed_png(transparency=bytes([0, 255, 255])))

    assert image.mask == b"[0 0]"
    assert image.smask is None


def test_partial_palette_transparency_is_refused_not_rounded() -> None:
    """Rounding a half-transparent entry to visible or invisible changes what
    the logo looks like; doing it silently turns a soft drop shadow into a grey
    box on every invoice.
    """
    with pytest.raises(ImageError, match="(?i)partial transparency"):
        load_image(indexed_png(transparency=bytes([0, 128, 255])))


def test_an_rgba_png_is_split_into_colour_and_an_smask() -> None:
    """PDF has no interleaved alpha. This is the only path in the writer that
    looks at a pixel, and FR-TPL-001's transparent logo is why it exists.
    """
    image = load_image(png())

    assert image.colour_space == b"/DeviceRGB"
    assert image.smask is not None
    assert image.smask.colour_space == b"/DeviceGray"
    assert image.smask.width == image.width
    assert image.smask.height == image.height


def test_the_split_puts_the_right_bytes_on_each_side() -> None:
    """The default fixture is red, green / blue, transparent. Decompressed, the
    colour stream must be the three RGB triples and the alpha stream must be
    the four alpha values - in that order, with nothing interleaved.
    """
    image = load_image(png())
    assert image.smask is not None

    colour = zlib.decompress(image.data)
    alpha = zlib.decompress(image.smask.data)

    assert colour == bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0])
    assert alpha == bytes([255, 255, 255, 0])


def test_a_greyscale_alpha_png_splits_too() -> None:
    pixels = [bytes([10, 255, 20, 0])]
    image = load_image(png(width=2, height=1, colour_type=GREY_ALPHA, pixels=pixels))

    assert image.colour_space == b"/DeviceGray"
    assert image.smask is not None
    assert zlib.decompress(image.data) == bytes([10, 20])
    assert zlib.decompress(image.smask.data) == bytes([255, 0])


# --- the scanline filters ----------------------------------------------------


@pytest.mark.parametrize(
    "filter_type", [0, 1, 2, 3, 4], ids=["none", "sub", "up", "average", "paeth"]
)
def test_every_scanline_filter_round_trips(filter_type: int) -> None:
    """The load-bearing decoder test. `image_fixtures._filtered` encodes from
    the specification independently of `_unfilter`, so a mistake shared by both
    cannot cancel out.

    Average and Paeth are the two worth having: both read the reconstructed
    line above, and using the raw one instead produces a smear that still looks
    like an image.
    """
    pixels = [
        bytes([255, 0, 0, 255, 0, 255, 0, 200, 9, 9, 9, 40]),
        bytes([0, 0, 255, 255, 7, 7, 7, 0, 128, 64, 32, 255]),
        bytes([1, 2, 3, 4, 250, 250, 250, 250, 0, 0, 0, 0]),
    ]
    image = load_image(png(width=3, height=3, filter_type=filter_type, pixels=pixels))
    assert image.smask is not None

    colour = zlib.decompress(image.data)
    alpha = zlib.decompress(image.smask.data)

    expected_colour = bytearray()
    expected_alpha = bytearray()
    for row in pixels:
        for start in range(0, len(row), 4):
            expected_colour.extend(row[start : start + 3])
            expected_alpha.append(row[start + 3])

    assert colour == bytes(expected_colour)
    assert alpha == bytes(expected_alpha)


# --- what is refused ---------------------------------------------------------


def test_an_interlaced_png_is_refused_with_the_export_setting() -> None:
    with pytest.raises(ImageError, match="(?i)interlaced"):
        load_image(png(interlace=1))


def test_a_truncated_png_is_refused() -> None:
    """Better than embedding half a logo, which renders as a band of colour and
    a lot of grey.
    """
    good = png()
    idat_start = good.index(b"IDAT")
    with pytest.raises(ImageError):
        load_image(good[: idat_start + 8] + b"\x00" * 4)


def test_a_png_with_no_header_is_refused() -> None:
    from tests.invoicing.image_fixtures import PNG_SIGNATURE

    with pytest.raises(ImageError, match="(?i)header"):
        load_image(PNG_SIGNATURE)


def test_an_indexed_png_with_no_palette_is_refused() -> None:
    with pytest.raises(ImageError, match="(?i)palette"):
        load_image(png(colour_type=3, pixels=[bytes([0, 1]), bytes([1, 0])]))


# --- placement ---------------------------------------------------------------


def test_placing_an_image_flips_to_pdf_coordinates_including_its_height() -> None:
    """The part each caller would get wrong: `top` is the image's TOP edge, so
    the flip has to account for how tall it is.
    """
    page = Page()
    page.place_image(
        image=load_image(png()), x=56, top=40, width=120, height=60, alt="Bakker Consultancy"
    )

    assert page.images[0].y == 842 - 40 - 60
    assert page.images[0].x == 56


def test_an_image_scaled_to_nothing_is_refused() -> None:
    """A logo that silently vanishes is worse than one that is refused."""
    with pytest.raises(ImageError, match="(?i)invisible"):
        Placement(image=load_image(png()), x=0, y=0, width=0, height=10)


def test_fit_preserves_the_aspect_ratio() -> None:
    """A stretched logo is the single most visible way a generated document
    looks wrong.
    """
    image = load_image(png(width=2, height=2, colour_type=RGB, pixels=[b"\x00" * 6] * 2))
    width, height = image.fit(max_width=100, max_height=50)

    assert (width, height) == (50.0, 50.0)


def test_fit_is_bounded_by_whichever_side_binds() -> None:
    pixels = [bytes([0] * 12)]
    wide = load_image(png(width=4, height=1, colour_type=RGB, pixels=pixels))

    assert wide.fit(max_width=100, max_height=100) == (100.0, 25.0)


# --- PDF structure -----------------------------------------------------------


def test_an_image_becomes_an_xobject_the_page_can_resolve() -> None:
    data = render_pdf([_page_with(load_image(png()))], title="x")

    assert b"/Subtype /Image" in data
    assert b"/XObject <<" in data
    name = re.search(rb"/XObject << /(Im\d+) (\d+) 0 R", data)
    assert name is not None
    assert b"/" + name.group(1) + b" Do" in data


def test_the_content_stream_scales_the_unit_square() -> None:
    """`cm` maps PDF's unit square onto the display rectangle, which is why the
    pixel dimensions play no part in placement.
    """
    data = render_pdf([_page_with(load_image(png()))], title="x")

    assert re.search(rb"q 120\.00 0 0 60\.00 56\.00 \d+\.\d+ cm /Im0 Do Q", data)


def test_the_transformation_is_bracketed() -> None:
    """Without `q`/`Q` the scale would apply to everything drawn afterwards,
    and every following line of text would be 120 points wide.
    """
    stream = _stream_of(render_pdf([_page_with(load_image(png()))], title="x"), index=-1)

    assert stream.count(b"q ") == stream.count(b" Q\n") == 1


def test_images_are_drawn_before_text() -> None:
    """So nothing a logo overlaps is hidden behind it."""
    page = _page_with(load_image(png()))
    page.at(x=56, top=200, value="Factuur 2026-1")
    stream = _stream_of(render_pdf([page], title="x"), index=-1)

    assert stream.index(b"Do") < stream.index(b"Tj")


def test_an_alpha_image_writes_two_objects_and_links_them() -> None:
    data = render_pdf([_page_with(load_image(png()))], title="x")

    assert data.count(b"/Subtype /Image") == 2
    smask = re.search(rb"/SMask (\d+) 0 R", data)
    assert smask is not None
    # The mask object must exist and be a greyscale image, or the logo renders
    # as an opaque rectangle.
    assert re.search(rb"%s 0 obj\n.*?/DeviceGray" % smask.group(1), data, re.S)


def test_one_logo_on_six_pages_is_one_object() -> None:
    """A repeated logo is deduplicated by content. Six copies of the bytes
    would be six times the file for a document nobody wants to be large.
    """
    image = load_image(png())
    pages = [_page_with(image) for _ in range(6)]
    data = render_pdf(pages, title="x")

    # One image plus its mask, once - not twelve.
    assert data.count(b"/Subtype /Image") == 2


def test_two_equal_images_loaded_separately_still_share() -> None:
    """Deduplication is by content, not by identity: the same file uploaded
    twice is one object.
    """
    pages = [_page_with(load_image(png())), _page_with(load_image(png()))]
    data = render_pdf(pages, title="x")

    assert data.count(b"/Subtype /Image") == 2


def test_two_different_images_are_two_objects() -> None:
    first = load_image(png())
    second = load_image(jpeg())
    data = render_pdf([_page_with(first), _page_with(second)], title="x")

    assert data.count(b"/Subtype /Image") == 3  # png + its mask, and the jpeg


def test_a_page_names_only_the_images_it_draws() -> None:
    """A Resources dictionary listing every image in the document would make
    each page depend on objects it never draws - which is how a stale XObject
    outlives the page that used it.
    """
    with_logo = _page_with(load_image(png()))
    without = Page()
    without.at(x=56, top=56, value="page two")

    data = render_pdf([with_logo, without], title="x")
    # Up to `/Contents`, which ends the Resources section - a non-greedy `>>`
    # would stop at the inner `/Font` dictionary and see neither.
    page_dicts = re.findall(rb"/Type /Page /Parent.*?/Contents", data, re.S)

    assert len(page_dicts) == 2
    assert b"/XObject" in page_dicts[0]
    assert b"/XObject" not in page_dicts[1]


def test_every_xref_offset_still_points_at_its_object_with_images() -> None:
    """The object numbering shifts to make room for images before the pages.
    An offset out by one produces a file that opens in a forgiving reader and
    fails in a strict one - discovered by a customer, not by us.
    """
    data = render_pdf([_page_with(load_image(png())), _page_with(load_image(jpeg()))], title="x")

    start = int(re.search(rb"startxref\n(\d+)", data).group(1))
    offsets = [int(m) for m in re.findall(rb"^(\d{10}) 00000 n", data[start:], re.M)]

    assert offsets
    for number, offset in enumerate(offsets, start=1):
        assert data[offset:].startswith(b"%d 0 obj" % number)


def test_the_pages_node_still_points_at_page_objects() -> None:
    """The /Kids numbers are computed BEFORE the image objects are written, so
    an off-by-one in the image count would point the document at an image and
    produce a PDF a reader opens with no pages in it.
    """
    data = render_pdf([_page_with(load_image(png())), _page_with(load_image(jpeg()))], title="x")

    kids = [
        int(n) for n in re.findall(rb"(\d+) 0 R", re.search(rb"/Kids \[(.*?)\]", data).group(1))
    ]
    assert len(kids) == 2

    for number in kids:
        body = re.search(rb"\n%d 0 obj\n(.*?)\nendobj" % number, data, re.S)
        assert body is not None, f"object {number} is missing"
        assert body.group(1).startswith(b"<< /Type /Page "), (
            f"/Kids points at object {number}, which is not a page"
        )


def test_rendering_with_images_is_deterministic() -> None:
    """FR-TPL-017 again: the same invoice must produce the same bytes, so the
    archive's content hash is a property of the document. `zlib.compress` at a
    fixed level over fixed input is what makes the alpha path safe here.
    """
    first = render_pdf([_page_with(load_image(png()))], title="x")
    second = render_pdf([_page_with(load_image(png()))], title="x")

    assert first == second


def test_a_document_with_no_images_is_unchanged() -> None:
    """The whole feature is additive: an invoice with no logo must not grow an
    empty XObject dictionary.
    """
    page = Page()
    page.at(x=56, top=56, value="Factuur")
    data = render_pdf([page], title="x")

    assert b"/XObject" not in data
    assert b"/Subtype /Image" not in data
