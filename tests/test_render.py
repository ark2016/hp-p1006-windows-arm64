import io
import struct
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from p1006.render import a4_page, file_pages, write_pbm, WIDTH, HEIGHT
from test_pwg import header


class RenderTests(unittest.TestCase):
    def test_transparent_pixels_print_white(self):
        for source in (Image.new("RGBA", (10, 10), (0, 0, 0, 0)),
                       Image.new("LA", (10, 10), (0, 0))):
            self.assertEqual(a4_page(source).getextrema(), (255, 255))

    def test_square_source_keeps_physical_square(self):
        page = a4_page(Image.new("L", (100, 100), 0))
        bounds = Image.eval(page, lambda x: 255 - x).getbbox()
        # The horizontal pixel density is twice the vertical density.
        self.assertEqual(bounds[2] - bounds[0], 2 * (bounds[3] - bounds[1]))

    def test_pwg_dimensions_are_not_fitted_as_square_pixels(self):
        source = Image.new("L", (WIDTH + 1, HEIGHT), 255)
        source.putpixel((200, 300), 0)
        page = a4_page(source, raster=True)
        self.assertEqual(page.size, (WIDTH, HEIGHT))
        self.assertEqual(page.getpixel((200, 300)), 0)
        with self.assertRaises(ValueError):
            a4_page(Image.new("L", (200, 200)), raster=True)

    def test_lower_dpi_pwg_preserves_one_inch_coordinates(self):
        for dpi in (300, 600):
            with self.subTest(dpi=dpi):
                source = Image.new("L", (WIDTH * dpi // 1200, HEIGHT * dpi // 600), 255)
                ImageDraw.Draw(source).rectangle((dpi, 2 * dpi, 2 * dpi - 1, 3 * dpi - 1), fill=0)
                page = a4_page(source, raster=True, resolution=(dpi, dpi))
                self.assertEqual(page.size, (WIDTH, HEIGHT))
                # One inch square, one inch from the left and two from the top.
                # Native pixels are half as wide as they are tall.
                black = page.point(lambda value: 255 if value < 128 else 0)
                self.assertEqual(black.getbbox(), (1200, 1200, 2400, 1800))
                source.close()
                page.close()
                black.close()

    def test_pwg_dimensions_are_checked_against_source_dpi(self):
        with self.assertRaisesRegex(ValueError, "Expected A4 PWG raster at 300x300"):
            a4_page(Image.new("L", (2480, 3500)), raster=True, resolution=(300, 300))
        with self.assertRaisesRegex(ValueError, "Unsupported source resolution"):
            a4_page(Image.new("L", (2480, 3508)), raster=True, resolution=(300, 600))

    def test_file_pages_reads_300dpi_from_real_pwg_header(self):
        # A4 at 300 dpi, black_1 with an eight-pixel stripe at x=400.
        # Encode runs independently from the decoder: 50 white bytes, FF,
        # 128+128+3 white bytes = 310 bytes/row = 2480 pixels.
        page_header = bytearray(header(2480, 3508))
        struct.pack_into(">II", page_header, 276, 300, 300)
        row = bytes([49, 0, 0, 255, 127, 0, 127, 0, 2, 0])
        data = b"RaS2" + page_header + (b"\xff" + row) * 13 + bytes([179]) + row
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.pwg"
            path.write_bytes(data)
            # The PWG header governs the pixels even if the IPP default is 600.
            page, = file_pages(path, resolution=(600, 600))
        black = page.point(lambda value: 255 if value < 128 else 0)
        self.assertEqual(black.getbbox(), (1600, 0, 1632, HEIGHT))
        self.assertEqual(page.size, (WIDTH, HEIGHT))
        page.close()
        black.close()

    def test_pbm_black_white_polarity(self):
        image = Image.new("1", (8, 1), 255)
        image.putpixel((0, 0), 0)
        target = io.BytesIO()
        write_pbm(image, target)
        self.assertEqual(target.getvalue(), b"P4\n8 1\n\x80")
