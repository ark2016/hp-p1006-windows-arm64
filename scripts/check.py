"""Run the offline suite and save an unambiguous UTF-8 verification report."""
import json
import os
import platform
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
(ROOT / "state").mkdir(exist_ok=True)
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
with (ROOT / "state/offline-tests.txt").open("w", encoding="utf-8") as log:
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
report = {
    "time_utc": datetime.now(timezone.utc).isoformat(),
    "python": sys.version,
    "os": platform.platform(),
    "tests_run": result.testsRun,
    "failures": len(result.failures),
    "errors": len(result.errors),
    "skipped": len(result.skipped),
    "successful": result.wasSuccessful(),
    "physical_print_verified": False,
    "windows_ipp_queue_verified": False,
    "scope": "Offline conversion, live Windows PDF rendering and loopback HTTP/IPP; USB API mocked except read-only discovery",
}
(ROOT / "state/verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=True, indent=2))
raise SystemExit(0 if result.wasSuccessful() else 1)
