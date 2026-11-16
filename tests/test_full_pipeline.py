"""HTTP -> IPP -> PWG -> XQX -> independent native decoder, with USB disabled."""
import functools
import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from p1006.firmware import ROOT
from p1006.ipp import create_server, encode_message, parse_message
from p1006.printing import process_document
from test_ipp import request
from test_pwg import header


class FullPipelineTests(unittest.TestCase):
    def test_http_raster_job_saves_exact_page_and_never_claims_printing(self):
        # A4 black_1: white except for an 8-pixel-wide stripe at x=400.
        # PWG runs: 50 zero bytes, one FF byte, 1189 zero bytes.
        row = bytes([49, 0, 0, 255]) + bytes([127, 0]) * 9 + bytes([36, 0])
        document = b"RaS2" + header(9920, 7016)
        document += (bytes([255]) + row) * 27 + bytes([103]) + row
        with tempfile.TemporaryDirectory(dir=ROOT / "state") as temp, patch("p1006.printing.submit_xqx") as usb:
            server = create_server(functools.partial(process_document, dry_run=True), Path(temp), port=0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = http.client.HTTPConnection(*server.server_address, timeout=15)
                client.request("POST", "/ipp/print", encode_message(request(2, data=document)),
                               {"Content-Type": "application/ipp"})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                message = parse_message(response.read())
                client.close()
                self.assertEqual(message.code, 0)
                server.bridge.queue.join()
                folder = Path(temp) / "jobs" / str(message.get("job-id"))
                saved = json.loads((folder / "job.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["state"], 8)  # dry run is deliberately NOT printed.
                self.assertFalse(saved["result"]["submitted"])
                usb.assert_not_called()
                decoded = subprocess.run([str(ROOT / "bin/xqxdecode.exe"), "-d", str(folder / "decoded")],
                                         input=(folder / "print.xqx").read_bytes(), capture_output=True, timeout=30)
                self.assertEqual(decoded.returncode, 0, decoded.stderr)
                paths = list(folder.glob("decoded-*.pbm"))
                self.assertEqual(len(paths), 1)
                with Image.open(paths[0]) as image:
                    self.assertEqual(image.size, (9600, 6848))
                    self.assertEqual(image.getpixel((224, 1000)), 0)
                    self.assertNotEqual(image.getpixel((223, 1000)), 0)
                    self.assertNotEqual(image.getpixel((232, 1000)), 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
