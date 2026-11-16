"""Exercise actual pythonw startup on an isolated port; never access USB."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent.parent
PYTHONW = Path(sys.executable).with_name("pythonw.exe")


@unittest.skipUnless(os.name == "nt" and PYTHONW.exists(), "Windows pythonw required")
class TestBackground(unittest.TestCase):
    def test_pythonw_serves_and_duplicate_exits_without_jobs(self):
        with tempfile.TemporaryDirectory(prefix="p1006-background-") as directory:
            isolated = Path(directory)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            harness = isolated / "launch.py"
            harness.write_text(
                "import importlib.util, sys\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "import p1006.__main__ as app\n"
                f"app.STATE = Path({str(isolated / 'state')!r})\n"
                "def reject_print(*args, **kwargs):\n"
                "    raise AssertionError('No print job is allowed in this test')\n"
                "app.process_document = reject_print\n"
                f"spec = importlib.util.spec_from_file_location('launcher', {str(ROOT / 'scripts/run-bridge-background.py')!r})\n"
                "launcher = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(launcher)\n"
                f"launcher.ROOT = Path({str(isolated)!r})\n"
                f"raise SystemExit(launcher.run(port={port}))\n",
                encoding="utf-8",
            )
            command = [str(PYTHONW), "-X", "utf8", str(harness)]
            first = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                opener = build_opener(ProxyHandler({}))
                ready = False
                for _ in range(100):
                    if first.poll() is not None:
                        self.fail("Background server exited before becoming ready")
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/ipp/print", timeout=.5) as response:
                            ready = response.read().startswith(b"HP LaserJet P1006 local IPP bridge.")
                    except OSError:
                        pass
                    if ready:
                        break
                    time.sleep(.05)
                self.assertTrue(ready)
                duplicate = subprocess.run(command, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(duplicate.returncode, 0)
                self.assertIsNone(first.poll())
                log = (isolated / "state/background.log").read_text(encoding="utf-8")
                self.assertIn("dry_run=False", log)
                self.assertIn("Bridge already responds", log)
                self.assertNotIn("Traceback", log)
                self.assertEqual(list((isolated / "state/ipp/jobs").iterdir()), [])
            finally:
                if first.poll() is None:
                    first.terminate()
                first.wait(timeout=10)
