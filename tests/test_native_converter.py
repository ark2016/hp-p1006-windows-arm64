"""Offline checks of the native tools; no printer or USB traffic is involved."""

from pathlib import Path
import hashlib
import random
import re
import struct
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENCODER = ROOT / "bin" / "foo2xqx.exe"
DECODER = ROOT / "bin" / "xqxdecode.exe"
FIRMWARE_WRAPPER = ROOT / "bin" / "arm2hpdl.exe"


class NativeConverterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for program in (ENCODER, DECODER, FIRMWARE_WRAPPER):
            if not program.is_file():
                raise RuntimeError("Build the native tools with scripts/build.ps1 first")
        (ROOT / ".test-output").mkdir(exist_ok=True)

    def run_tool(self, program, *args, data=None):
        result = subprocess.run(
            [str(program), *map(str, args)],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout

    def roundtrip(self, width, height, payloads, resolution, file_input=False, clip=None):
        pages = b"".join(
            f"P4\n{width} {height}\n".encode("ascii") + pixels for pixels in payloads
        )
        with tempfile.TemporaryDirectory(prefix="native-", dir=ROOT / ".test-output") as work:
            work = Path(work)
            source = work / "source.pbm"
            source.write_bytes(pages)
            arguments = [f"-r{resolution}", f"-g{width}x{height}", "-p9"]
            if clip:
                left, top, right, bottom = clip
                arguments += [f"-u{left}x{top}", f"-l{right}x{bottom}"]
            else:
                left = top = right = bottom = 0
            if file_input:
                encoded = self.run_tool(ENCODER, *arguments, source)
            else:
                encoded = self.run_tool(ENCODER, *arguments, data=pages)
            self.assertIn(b",XQX", encoded)
            stream = work / "job.xqx"
            stream.write_bytes(encoded)
            prefix = work / "page"
            if file_input:
                listing = self.run_tool(DECODER, "-d", prefix, stream)
            else:
                listing = self.run_tool(DECODER, "-d", prefix, data=encoded)
            self.assertEqual(listing.count(b"XQX_START_PAGE("), len(payloads))
            self.assertIn(b"XQX_END_DOC", listing)
            outputs = sorted(work.glob("page-*.pbm"))
            self.assertEqual(len(outputs), len(payloads))
            for output, original in zip(outputs, payloads):
                decoded = output.read_bytes()
                header = re.match(rb"P4\n\s*(\d+)\s+(\d+)\n", decoded)
                self.assertIsNotNone(header, decoded[:100])
                decoded_width, decoded_height = map(int, header.groups())
                expected_width = width - left - right
                expected_height = height - top - bottom
                # XQX stores each row in 128-bit blocks; the extra pixels are white.
                padded_width = (expected_width + 127) // 128 * 128
                self.assertEqual((decoded_width, decoded_height), (padded_width, expected_height))
                self.assertEqual(left % 8, 0, "Test crop must be byte-aligned")
                self.assertEqual(expected_width % 8, 0, "Test crop must be byte-aligned")
                row_bytes = width // 8
                expected = b"".join(
                    original[y * row_bytes + left // 8:y * row_bytes + (width - right) // 8]
                    + bytes((padded_width - expected_width) // 8)
                    for y in range(top, height - bottom)
                )
                self.assertEqual(decoded[header.end():], expected)
            return listing

    def test_binary_pipes_preserve_every_byte_at_600dpi(self):
        # Includes CR, LF, Ctrl-Z, NUL and all high bytes: text-mode CRT breaks this.
        self.roundtrip(256, 128, [bytes(range(256)) * 16], "600x600")

    def test_files_multipage_jbig_chunks_at_1200x600(self):
        # Incompressible data exceeds the encoder's 65536-byte JBIG chunks.
        rng = random.Random(1006)
        payloads = [rng.randbytes(2048 // 8 * 513) for _ in range(2)]
        listing = self.roundtrip(2048, 513, payloads, "1200x600", file_input=True)
        self.assertGreater(listing.count(b"XQX_JBIG("), len(payloads))
        self.assertIn(b"XQXI_VIDEO_BPP, 2", listing)

    def test_a4_default_geometry_and_margins_at_1200x600(self):
        width, height = 9920, 7016
        row = (bytes(range(256)) * 5)[:width // 8]
        pixels = row * height
        self.roundtrip(width, height, [pixels], "1200x600", clip=(176, 84, 176, 84))

    def test_p1006_firmware_wrapper_preserves_date_and_binary_payload(self):
        with tempfile.TemporaryDirectory(prefix="firmware-", dir=ROOT / ".test-output") as work:
            source = Path(work) / "P1006.img"
            original = b"20100824\x7fELF" + bytes(range(256)) * 3
            source.write_bytes(original)
            actual = self.run_tool(FIRMWARE_WRAPPER, source)
            expected = (
                b"\x1b%-12345X@PJL ENTER LANGUAGE=ACL\r\n"
                + b"\x00\xac\xc0\xde" + struct.pack(">I", len(original) - 8)
                + original + b"\x1b%-12345X"
            )
            self.assertEqual(actual, expected)

    def test_user_firmware_matches_known_image_and_wrapper(self):
        from p1006.firmware import firmware_bytes

        source = ROOT / ".vendor" / "hp" / "P1006.img"
        if not source.is_file():
            self.skipTest("The user's optional HP installer has not been extracted")
        original = source.read_bytes()
        self.assertEqual(len(original), 223487)
        self.assertEqual(
            hashlib.sha256(original).hexdigest(),
            "4031deea33ca48d66d93429f3f847b43c46446b8438e2717da9a9a1ea8e43669",
        )
        actual = self.run_tool(FIRMWARE_WRAPPER, source)
        expected = (
            b"\x1b%-12345X@PJL ENTER LANGUAGE=ACL\r\n"
            + b"\x00\xac\xc0\xde" + struct.pack(">I", len(original) - 8)
            + original + b"\x1b%-12345X"
        )
        self.assertEqual(actual, expected)
        self.assertEqual(firmware_bytes(source), actual)


if __name__ == "__main__":
    unittest.main(verbosity=2)
