"""Real PNG and JPEG bytes, built from scratch - FR-TPL-001.

There is no image library in this project (that is the whole reason
`api.invoicing.pdf` embeds rather than decodes), so the fixtures are written
here rather than loaded. That is a feature as much as a constraint: every byte
a test asserts against is one this file put there, so a failure points at the
decoder rather than at a checked-in blob nobody can read.

The PNGs are genuine - correct CRCs, real zlib streams, real scanline filters -
because a decoder tested against a fake is tested against nothing. `filter_type`
is a parameter so the Paeth and Average predictors, which are the two easy ones
to get subtly wrong, are exercised on the same pixels as the trivial ones.
"""

from __future__ import annotations

import struct
import zlib

__all__ = [
    "png",
    "indexed_png",
    "jpeg",
    "PNG_SIGNATURE",
]

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: PNG colour types, by channel count, so a caller reads `RGBA` rather than 6.
GREY, RGB, INDEXED, GREY_ALPHA, RGBA = 0, 2, 3, 4, 6


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def _filtered(rows: list[bytes], *, filter_type: int, bpp: int) -> bytes:
    """Apply one PNG scanline filter to every row.

    The forward direction of `api.invoicing.pdf._unfilter`, written
    independently from the specification rather than by inverting that
    function - otherwise a mistake shared by both would cancel out and the
    round trip would prove nothing.
    """
    out = bytearray()
    previous = bytes(len(rows[0])) if rows else b""

    for row in rows:
        out.append(filter_type)
        encoded = bytearray(len(row))
        for index, value in enumerate(row):
            left = row[index - bpp] if index >= bpp else 0
            above = previous[index]
            upper_left = previous[index - bpp] if index >= bpp else 0

            if filter_type == 0:
                encoded[index] = value
            elif filter_type == 1:
                encoded[index] = (value - left) & 0xFF
            elif filter_type == 2:
                encoded[index] = (value - above) & 0xFF
            elif filter_type == 3:
                encoded[index] = (value - ((left + above) >> 1)) & 0xFF
            elif filter_type == 4:
                estimate = left + above - upper_left
                da, db, dc = (
                    abs(estimate - left),
                    abs(estimate - above),
                    abs(estimate - upper_left),
                )
                if da <= db and da <= dc:
                    nearest = left
                elif db <= dc:
                    nearest = above
                else:
                    nearest = upper_left
                encoded[index] = (value - nearest) & 0xFF
            else:  # pragma: no cover - the fixtures never ask for one
                raise ValueError(f"no such PNG filter: {filter_type}")

        out.extend(encoded)
        previous = row

    return bytes(out)


def png(
    *,
    width: int = 2,
    height: int = 2,
    colour_type: int = RGBA,
    bits: int = 8,
    filter_type: int = 0,
    interlace: int = 0,
    pixels: list[bytes] | None = None,
    palette: bytes | None = None,
    transparency: bytes | None = None,
) -> bytes:
    """One PNG. Defaults to a 2x2 RGBA square with a transparent corner.

    Transparent by default because that is what a logo is (FR-TPL-001 asks for
    "a transparent-background preview"), and because the alpha path is the only
    one in the writer that decodes.
    """
    channels = {GREY: 1, RGB: 3, INDEXED: 1, GREY_ALPHA: 2, RGBA: 4}[colour_type]
    sample = bits // 8 or 1

    if pixels is None:
        # Opaque red, opaque green / opaque blue, fully transparent.
        rows = [
            bytes([255, 0, 0, 255, 0, 255, 0, 255]),
            bytes([0, 0, 255, 255, 0, 0, 0, 0]),
        ]
        if colour_type != RGBA or (width, height) != (2, 2):  # pragma: no cover
            raise ValueError("supply `pixels` for anything but the default RGBA square")
    else:
        rows = pixels

    body = _filtered(rows, filter_type=filter_type, bpp=max(1, channels * sample))

    header = struct.pack(">IIBBBBB", width, height, bits, colour_type, 0, 0, interlace)
    out = PNG_SIGNATURE + _chunk(b"IHDR", header)
    if palette is not None:
        out += _chunk(b"PLTE", palette)
    if transparency is not None:
        out += _chunk(b"tRNS", transparency)
    out += _chunk(b"IDAT", zlib.compress(body, 9))
    return out + _chunk(b"IEND", b"")


def indexed_png(
    *,
    transparency: bytes | None = None,
    bits: int = 8,
    width: int = 2,
    height: int = 2,
) -> bytes:
    """A palette PNG: index 0 red, index 1 green, index 2 blue."""
    return png(
        width=width,
        height=height,
        colour_type=INDEXED,
        bits=bits,
        pixels=[bytes([0, 1]), bytes([2, 0])],
        palette=bytes([255, 0, 0, 0, 255, 0, 0, 0, 255]),
        transparency=transparency,
    )


def jpeg(
    *,
    width: int = 4,
    height: int = 3,
    components: int = 3,
    precision: int = 8,
    marker: int = 0xC0,
    adobe: bool = False,
) -> bytes:
    """A JPEG carrying a real frame header and a stub scan.

    The entropy-coded data is not a decodable image, and deliberately: the
    writer never decodes a JPEG - it copies the file into the stream - so what
    a test needs is a header the scanner must walk past segments to reach.

    `marker` is the SOF, so a caller can produce the progressive variant
    (`0xC2`) the writer refuses.
    """
    out = bytearray(b"\xff\xd8")

    if adobe:
        payload = b"Adobe" + bytes([0, 100, 0, 0, 0, 0, 0, 0, 2])
        out += b"\xff\xee" + struct.pack(">H", len(payload) + 2) + payload

    # A comment segment before the frame, so the scanner has something with a
    # length to skip - the step that goes wrong when segment walking is naive.
    comment = b"ledgr fixture"
    out += b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment

    frame = struct.pack(">BHHB", precision, height, width, components)
    # One component descriptor per channel: id, sampling factors, quant table.
    frame += bytes([1, 0x11, 0]) * components
    out += bytes([0xFF, marker]) + struct.pack(">H", len(frame) + 2) + frame

    # Start of scan, then stub entropy data and the end marker.
    scan = bytes([components]) + bytes([1, 0]) * components + bytes([0, 63, 0])
    out += b"\xff\xda" + struct.pack(">H", len(scan) + 2) + scan
    out += b"\x00" * 8 + b"\xff\xd9"
    return bytes(out)
