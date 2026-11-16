import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from p1006.firmware import ROOT
from p1006.printing import encode_pages, STATE
from p1006.render import WIDTH, HEIGHT


class PrintingTests(unittest.TestCase):
    def test_multipage_copies_are_collated_in_the_actual_xqx_stream(self):
        STATE.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=STATE) as temporary:
            directory = Path(temporary)
            def pages():
                for x in (400, 800):
                    page = Image.new("1", (WIDTH, HEIGHT), 255)
                    page.putpixel((x, 300), 0)
                    yield page
            output = directory / "collated.xqx"
            result = encode_pages(pages(), output, copies=2)
            self.assertEqual((result["pages"], result["copies"]), (2, 2))
            decoded = subprocess.run([str(ROOT / "bin/xqxdecode.exe"), "-d", str(directory / "page")],
                                     input=output.read_bytes(), capture_output=True, timeout=30)
            self.assertEqual(decoded.returncode, 0, decoded.stderr)
            files = sorted(directory.glob("page-*.pbm"))
            self.assertEqual(len(files), 4)
            for path, x in zip(files, (400, 800, 400, 800)):
                with Image.open(path) as page:
                    self.assertEqual(page.getpixel((x - 176, 300 - 84)), 0)
                    other = 800 if x == 400 else 400
                    self.assertNotEqual(page.getpixel((other - 176, 300 - 84)), 0)


if __name__ == "__main__":
    unittest.main()
