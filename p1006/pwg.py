"""Bounded PWG Raster decoder for the monochrome P1006 bridge.

Reference: PWG 5102.4-2012, sections 4.3 and 4.4:
https://ftp.pwg.org/pub/pwg/candidates/cs-ippraster10-20120420-5102.4.pdf
No CUPS/PWG writer is used here; callers receive one Pillow image at a time.
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from PIL import Image, ImageOps


class RasterError(ValueError):
    """Unsupported or malformed PWG Raster."""


@dataclass
class RasterPage:
    image: Image.Image
    resolution: tuple[int, int]
    page_size_points: tuple[int, int]


def _exact(stream: BinaryIO, length: int) -> bytes:
    data = stream.read(length)
    if len(data) != length:
        raise RasterError("Truncated PWG Raster")
    return data


def _decode_row(stream: BinaryIO, length: int, pixel_bytes: int = 1) -> bytes:
    """Decode one row, excluding its preceding repeat count (PWG 4.4)."""
    row = bytearray()
    while len(row) < length:
        control = _exact(stream, 1)[0]
        # 128 is not a PWG run. Some CUPS readers accept it as clear-to-EOL;
        # deliberately do not silently accept that non-PWG extension.
        if control == 128:
            raise RasterError("Reserved PWG run control 128")
        count = control + 1 if control < 128 else 257 - control
        byte_count = count * pixel_bytes
        if byte_count > length - len(row):
            raise RasterError("PWG run exceeds scanline")
        if control < 128:
            row.extend(_exact(stream, pixel_bytes) * count)
        else:
            row.extend(_exact(stream, byte_count))
    return bytes(row)


def iter_pages(data: bytes | BinaryIO, *, max_pages: int = 1000,
               max_page_pixels: int = 100_000_000) -> Iterator[RasterPage]:
    """Read big-endian RaS2 black_1/sgray_1/sgray_8 pages.

    PWG pages are whole-sheet images; image-box fields are metadata, not a
    crop. Only front-side simplex transforms are accepted. The limit bounds
    allocation per page; callers should consume this iterator incrementally.
    """
    stream = io.BytesIO(data) if isinstance(data, bytes) else data
    if _exact(stream, 4) != b"RaS2":
        raise RasterError("Expected big-endian PWG Raster RaS2 signature")
    page_count = 0
    while True:
        first = stream.read(1)
        if not first:
            if page_count == 0:
                raise RasterError("PWG Raster contains no pages")
            return
        page_count += 1
        if page_count > max_pages:
            raise RasterError("PWG page count exceeds limit")
        header = first + _exact(stream, 1795)
        if header[:64].split(b"\0", 1)[0] != b"PwgRaster":
            raise RasterError("Expected PwgRaster media class")
        u32 = lambda offset: struct.unpack_from(">I", header, offset)[0]
        s32 = lambda offset: struct.unpack_from(">i", header, offset)[0]
        width, height = u32(372), u32(376)
        bits, pixel_bits, stride = u32(384), u32(388), u32(392)
        color_order, color_space, colors = u32(396), u32(400), u32(420)
        xdpi, ydpi = u32(276), u32(280)
        if not (width and height and width * height <= max_page_pixels):
            raise RasterError("Invalid or excessive PWG page dimensions")
        if not (1 <= xdpi <= 2400 and 1 <= ydpi <= 2400):
            raise RasterError("Invalid PWG resolution")
        if color_order != 0 or colors != 1 or bits != pixel_bits:
            raise RasterError("Only single-channel chunky PWG Raster is supported")
        if (color_space, bits) not in {(3, 1), (18, 1), (18, 8)}:
            raise RasterError("Supported PWG types: black_1, sgray_1, sgray_8")
        if stride != (width * bits + 7) // 8:
            raise RasterError("PWG BytesPerLine does not match page width")
        if u32(272) or u32(368):
            raise RasterError("Duplex PWG Raster is unsupported")
        if s32(456) != 1 or s32(460) != 1:
            raise RasterError("PWG front-side transforms must both equal 1")
        raster = bytearray()
        rows = 0
        while rows < height:
            repeats = _exact(stream, 1)[0] + 1
            if rows + repeats > height:
                raise RasterError("PWG row repetition exceeds page height")
            row = _decode_row(stream, stride)
            raster.extend(row * repeats)
            rows += repeats
        mode = "1" if bits == 1 else "L"
        image = Image.frombytes(mode, (width, height), bytes(raster))
        if color_space == 3:
            # black_1 uses 1=black; Pillow 1 and sgray use 1=white.
            image = ImageOps.invert(image.convert("L")).convert("1")
        image.info["dpi"] = (xdpi, ydpi)
        yield RasterPage(image, (xdpi, ydpi), (u32(352), u32(356)))


def read_pwg(data: bytes, **limits) -> list[RasterPage]:
    """Convenience for small documents; use iter_pages for real print jobs."""
    return list(iter_pages(data, **limits))
