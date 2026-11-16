"""Live built-in Windows PDF rendering, with an independent vector PDF fixture."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from p1006.firmware import ROOT
from p1006.printing import process_document
from p1006.render import a4_page, pdf_pages, WIDTH, HEIGHT


def vector_pdf():
    content = b"0 0 0 rg 50 700 100 50 re f\n"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << >> /Contents 5 0 R >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << >> /Contents 6 0 R >>",
               b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
               b"<< /Length 0 >>\nstream\nendstream"]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


@unittest.skipUnless(os.name == "nt", "Uses Windows.Data.Pdf")
class WindowsPdfTests(unittest.TestCase):
    def test_300dpi_job_renders_at_requested_dpi_and_encodes_native_geometry(self):
        (ROOT / "state").mkdir(exist_ok=True)
        source_sizes = []

        def normalize(image, **kwargs):
            source_sizes.append(image.size)
            return a4_page(image, **kwargs)

        with tempfile.TemporaryDirectory(dir=ROOT / "state") as temporary:
            folder = Path(temporary)
            with patch("p1006.render.a4_page", side_effect=normalize), patch("p1006.printing.submit_xqx") as usb:
                result = process_document(vector_pdf(), "application/pdf",
                                          {"job-dir": str(folder), "printer-resolution": (300, 300, 3)},
                                          dry_run=True)
            usb.assert_not_called()
            self.assertEqual(len(source_sizes), 2)
            for width, height in source_sizes:
                self.assertTrue(2470 <= width <= 2480 and 3500 <= height <= 3508, (width, height))
            self.assertEqual(result["resolution"], [1200, 600])
            self.assertEqual(result["pages"], 2)
            self.assertFalse(result["submitted"])
            decoded = subprocess.run([str(ROOT / "bin/xqxdecode.exe"), "-d", str(folder / "decoded")],
                                     input=(folder / "print.xqx").read_bytes(), capture_output=True, timeout=30)
            self.assertEqual(decoded.returncode, 0, decoded.stderr)
            images = sorted(folder.glob("decoded-*.pbm"))
            self.assertEqual(len(images), 2)
            with Image.open(images[0]) as first:
                self.assertEqual(first.getpixel((1667 - 176, 975 - 84)), 0)
                self.assertNotEqual(first.getpixel((4000 - 176, 4000 - 84)), 0)
            with Image.open(images[1]) as second:
                self.assertEqual(second.getextrema(), (255, 255))

    def test_two_vector_pages_geometry_and_white_background(self):
        (ROOT / "state").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "state") as temporary:
            source = Path(temporary) / "two-pages.pdf"
            source.write_bytes(vector_pdf())
            pages = pdf_pages(source)
            first = next(pages)
            self.assertEqual(first.size, (WIDTH, HEIGHT))
            self.assertEqual(first.getpixel((1667, 975)), 0)  # Inside PDF rectangle.
            self.assertEqual(first.getpixel((4000, 4000)), 255)
            del first
            second = next(pages)
            self.assertEqual(second.getextrema(), (255, 255))
            with self.assertRaises(StopIteration):
                next(pages)
