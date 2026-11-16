"""Wire fixtures follow PWG 5102.4-2012, not an in-project raster encoder."""
import io
import struct
import unittest

from p1006.pwg import RasterError, _decode_row, iter_pages


def header(width=8, height=2, bits=1, space=3):
    # Independent field offsets from PWG 5102.4 section 4.3/Table 1.
    value = bytearray(1796)
    value[:9] = b"PwgRaster"
    for offset, number in {276:1200, 280:600, 352:595, 356:842,
                           372:width, 376:height, 384:bits, 388:bits,
                           392:(width * bits + 7)//8, 396:0, 400:space,
                           420:1, 456:1, 460:1}.items():
        struct.pack_into(">I", value, offset, number)
    return bytes(value)


class TestPWG(unittest.TestCase):
    def test_published_cups_89_octet_example(self):
        # Exact sample from OpenPrinting's CUPS Raster Format / Figure 3:
        # https://openprinting.github.io/cups/doc/spec-raster.html
        # Exercises run-lengths, literals, multi-byte colors and repeated rows.
        wire = bytes.fromhex("""
            00 00FFFFFF 02FFFF00 03FFFFFF
            00 FEFFFF000000FFFFFF00 02FFFFFF 0000FF00 00FFFFFF
            00 01FFFF00 02FFFFFF 0200FF00
            00 02FFFF00 02FFFFFF 0000FF00 00FFFFFF
            00 00FFFFFF 02FFFF00 03FFFFFF
            00 07FFFFFF
            01 07FF0000
        """)
        self.assertEqual(len(wire), 89)
        stream = io.BytesIO(wire)
        rows = []
        while stream.tell() < len(wire):
            repeat = stream.read(1)[0] + 1
            rows.extend([_decode_row(stream, 24, 3)] * repeat)
        W, Y, B, G, R = (bytes.fromhex(x) for x in ("ffffff", "ffff00", "0000ff", "00ff00", "ff0000"))
        expected = [W+Y*3+W*4, Y+B+Y+W*3+G+W, Y*2+W*3+G*3,
                    Y*3+W*3+G+W, W+Y*3+W*4, W*8, R*8, R*8]
        self.assertEqual(rows, expected)

    def test_black_1_polarity_and_row_repetition(self):
        # Two copies of 10000001; black_1 defines high bit = black.
        page, = iter_pages(b"RaS2" + header() + bytes.fromhex("01 00 81"))
        self.assertEqual(page.resolution, (1200, 600))
        self.assertEqual(page.page_size_points, (595, 842))
        self.assertEqual(list(page.image.convert("L").tobytes()), [0]+[255]*6+[0]+[0]+[255]*6+[0])

    def test_sgray1_uses_white_one(self):
        page, = iter_pages(b"RaS2" + header(space=18) + bytes.fromhex("01 00 81"))
        self.assertEqual(list(page.image.convert("L").tobytes())[:8], [255]+[0]*6+[255])

    def test_sgray8_literal_and_multiple_pages(self):
        first = header(width=4, height=1, bits=8, space=18) + bytes.fromhex("00 FD 00 40 80 FF")
        second = header(width=8, height=1) + bytes.fromhex("00 00 00")
        pages = list(iter_pages(b"RaS2" + first + second))
        self.assertEqual(list(pages[0].image.tobytes()), [0,64,128,255])
        self.assertEqual(pages[1].image.getextrema(), (255,255))

    def test_non_byte_aligned_width(self):
        page, = iter_pages(b"RaS2" + header(width=9, height=1) + bytes.fromhex("00 FF 80 80"))
        self.assertEqual(list(page.image.convert("L").tobytes()), [0]+[255]*7+[0])

    def test_rejects_truncation_and_bad_runs(self):
        for suffix in (b"", b"\0", b"\0\0", b"\2\0\xff", b"\1\1\xff", b"\1\x80"):
            with self.subTest(suffix=suffix), self.assertRaises(RasterError):
                list(iter_pages(b"RaS2" + header() + suffix))

    def test_rejects_wrong_format_color_transform_and_large_page(self):
        fixtures = [b"2SaR" + header(), b"RaS2", b"RaS2" + header(space=19),
                    b"RaS2" + header(width=100_000, height=100_000)]
        transformed = bytearray(header())
        struct.pack_into(">i", transformed, 456, -1)
        fixtures.append(b"RaS2" + transformed)
        for fixture in fixtures:
            with self.subTest(size=len(fixture)), self.assertRaises(RasterError):
                list(iter_pages(fixture))


if __name__ == "__main__":
    unittest.main()
