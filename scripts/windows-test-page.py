"""Print one GDI page through the explicitly named Windows IPP queue.

Use --inspect-only to inspect the queue without creating a GDI DC or a job.
The script never selects the default printer and never accesses USB directly.

API references:
https://learn.microsoft.com/windows/win32/printdocs/printer-info-2
https://learn.microsoft.com/windows/win32/api/wingdi/ns-wingdi-docinfow
https://learn.microsoft.com/windows/win32/api/wingdi/nf-wingdi-startdocw
https://learn.microsoft.com/windows/win32/api/wingdi/nf-wingdi-createdcw
"""

from __future__ import annotations

import argparse
import ctypes as C
import datetime as dt
import json
import os
from pathlib import Path
import sys


QUEUE = "HP LaserJet P1006 (ARM bridge)"
DRIVER = "Microsoft IPP Class Driver"
JOB_NAME = "P1006 Windows queue verification"
REPORT = Path(__file__).resolve().parents[1] / "state/windows-test-transfer.json"
DWORD, WORD, SHORT, INT = C.c_uint32, C.c_uint16, C.c_int16, C.c_int32
HANDLE, LPCWSTR, BOOL = C.c_void_p, C.c_wchar_p, C.c_int32


class PRINTER_INFO_2W(C.Structure):
    _fields_ = [
        ("pServerName", LPCWSTR), ("pPrinterName", LPCWSTR),
        ("pShareName", LPCWSTR), ("pPortName", LPCWSTR),
        ("pDriverName", LPCWSTR), ("pComment", LPCWSTR),
        ("pLocation", LPCWSTR), ("pDevMode", C.c_void_p),
        ("pSepFile", LPCWSTR), ("pPrintProcessor", LPCWSTR),
        ("pDatatype", LPCWSTR), ("pParameters", LPCWSTR),
        ("pSecurityDescriptor", C.c_void_p), ("Attributes", DWORD),
        ("Priority", DWORD), ("DefaultPriority", DWORD),
        ("StartTime", DWORD), ("UntilTime", DWORD),
        ("Status", DWORD), ("cJobs", DWORD), ("AveragePPM", DWORD),
    ]


class DOCINFOW(C.Structure):
    _fields_ = [("cbSize", INT), ("lpszDocName", LPCWSTR),
                ("lpszOutput", LPCWSTR), ("lpszDatatype", LPCWSTR),
                ("fwType", DWORD)]


class DEVMODE_PRINTER_PREFIX(C.Structure):
    # Fixed Windows WCHAR[32] representation, independent of host wchar size.
    # This is only the printer prefix; preserve the remainder and driver data.
    _fields_ = [("dmDeviceName", WORD * 32), ("dmSpecVersion", WORD),
                ("dmDriverVersion", WORD), ("dmSize", WORD),
                ("dmDriverExtra", WORD), ("dmFields", DWORD),
                ("dmOrientation", SHORT), ("dmPaperSize", SHORT),
                ("dmPaperLength", SHORT), ("dmPaperWidth", SHORT),
                ("dmScale", SHORT), ("dmCopies", SHORT),
                ("dmDefaultSource", SHORT), ("dmPrintQuality", SHORT)]


def api():
    if os.name != "nt":
        raise RuntimeError("This verification requires Windows.")
    spool = C.WinDLL("winspool.drv", use_last_error=True)
    gdi = C.WinDLL("gdi32", use_last_error=True)
    bindings = [
        (spool, "OpenPrinterW", BOOL, [LPCWSTR, C.POINTER(HANDLE), C.c_void_p]),
        (spool, "GetPrinterW", BOOL, [HANDLE, DWORD, C.c_void_p, DWORD, C.POINTER(DWORD)]),
        (spool, "ClosePrinter", BOOL, [HANDLE]),
        (gdi, "CreateDCW", HANDLE, [LPCWSTR, LPCWSTR, LPCWSTR, C.c_void_p]),
        (gdi, "DeleteDC", BOOL, [HANDLE]),
        (gdi, "GetDeviceCaps", INT, [HANDLE, INT]),
        (gdi, "StartDocW", INT, [HANDLE, C.POINTER(DOCINFOW)]),
        (gdi, "StartPage", INT, [HANDLE]),
        (gdi, "EndPage", INT, [HANDLE]),
        (gdi, "EndDoc", INT, [HANDLE]),
        (gdi, "AbortDoc", INT, [HANDLE]),
        (gdi, "CreateFontW", HANDLE,
         [INT, INT, INT, INT, INT, DWORD, DWORD, DWORD, DWORD, DWORD,
          DWORD, DWORD, DWORD, LPCWSTR]),
        (gdi, "SelectObject", HANDLE, [HANDLE, HANDLE]),
        (gdi, "DeleteObject", BOOL, [HANDLE]),
        (gdi, "GetStockObject", HANDLE, [INT]),
        (gdi, "SetBkMode", INT, [HANDLE, INT]),
        (gdi, "SetTextColor", DWORD, [HANDLE, DWORD]),
        (gdi, "TextOutW", BOOL, [HANDLE, INT, INT, LPCWSTR, INT]),
        (gdi, "Rectangle", BOOL, [HANDLE, INT, INT, INT, INT]),
    ]
    for dll, name, result, arguments in bindings:
        function = getattr(dll, name)
        function.restype, function.argtypes = result, arguments
    return spool, gdi


def fail(call: str):
    code = C.get_last_error()
    detail = C.FormatError(code).strip() if code else "the API returned failure without an error code"
    raise OSError(code, f"{call}: {detail}")


def positive(result: int, call: str) -> int:
    if result <= 0:
        fail(call)
    return result


def write_report(report: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    report["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    temporary = REPORT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, REPORT)


def inspect_queue(spool, handle) -> tuple[dict, C.Array]:
    needed = DWORD()
    if not spool.GetPrinterW(handle, 2, None, 0, C.byref(needed)):
        if C.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
            fail("GetPrinterW(size)")
    if not C.sizeof(PRINTER_INFO_2W) <= needed.value <= 16 * 1024 * 1024:
        raise RuntimeError(f"Unexpected GetPrinterW buffer size: {needed.value}")
    buffer = C.create_string_buffer(needed.value)
    if not spool.GetPrinterW(handle, 2, buffer, len(buffer), C.byref(needed)):
        fail("GetPrinterW")
    info = C.cast(buffer, C.POINTER(PRINTER_INFO_2W)).contents
    metadata = {"queue": info.pPrinterName, "driver": info.pDriverName,
                "port": info.pPortName, "server": info.pServerName,
                "print_processor": info.pPrintProcessor, "default_datatype": info.pDatatype,
                "queue_status": info.Status, "queued_jobs_before": info.cJobs,
                "separator_file": info.pSepFile}
    if (info.pPrinterName or "").casefold() != QUEUE.casefold():
        raise RuntimeError(f"Queue identity mismatch: {info.pPrinterName!r}")
    if (info.pDriverName or "").casefold() != DRIVER.casefold():
        raise RuntimeError(f"Expected {DRIVER!r}; found {info.pDriverName!r}. No job created.")
    if not info.pDevMode:
        raise RuntimeError("The queue exposes no default DEVMODE; cannot verify one-copy settings.")
    prefix = C.cast(info.pDevMode, C.POINTER(DEVMODE_PRINTER_PREFIX)).contents
    if prefix.dmSize < C.sizeof(DEVMODE_PRINTER_PREFIX):
        raise RuntimeError("The queue returned an incomplete printer DEVMODE.")
    length = prefix.dmSize + prefix.dmDriverExtra
    devmode = C.create_string_buffer(C.string_at(info.pDevMode, length), length)
    settings = DEVMODE_PRINTER_PREFIX.from_buffer(devmode)
    metadata["default_settings"] = {
        "fields": settings.dmFields, "orientation": settings.dmOrientation,
        "paper_size": settings.dmPaperSize, "copies": settings.dmCopies,
        "devmode_bytes": length,
    }
    # A per-job copy of the queue defaults: retain A4, resolution and orientation,
    # but request exactly one copy without changing persistent queue settings.
    settings.dmFields |= 0x100  # DM_COPIES
    settings.dmCopies = 1
    return metadata, devmode


def run(inspect_only: bool, report: dict) -> None:
    spool, gdi = api()
    printer = HANDLE()
    dc = None
    font = None
    old_font = None
    old_brush = None
    started = False
    try:
        report["phase"] = "open-explicit-queue"
        if not spool.OpenPrinterW(QUEUE, C.byref(printer), None):
            fail("OpenPrinterW")
        metadata, devmode = inspect_queue(spool, printer)
        report.update(metadata)
        report["phase"] = "queue-verified"
        if inspect_only:
            return
        if report["separator_file"]:
            raise RuntimeError("The queue has a separator page configured; refusing to print extra paper.")
        # The queue and its IPP class driver have been verified before any GDI DC.
        dc = gdi.CreateDCW("WINSPOOL", QUEUE, None, devmode)
        if not dc:
            fail("CreateDCW")
        caps = {name: gdi.GetDeviceCaps(dc, code) for name, code in {
            "printable_width": 8, "printable_height": 10,
            "dpi_x": 88, "dpi_y": 90, "physical_width": 110,
            "physical_height": 111, "offset_x": 112, "offset_y": 113,
        }.items()}
        if any(caps[key] <= 0 for key in (
                "dpi_x", "dpi_y", "printable_width", "printable_height", "physical_width", "physical_height")):
            raise RuntimeError(f"Invalid GDI printer metrics: {caps}")
        dx, dy = caps["dpi_x"], caps["dpi_y"]
        size_mm = [caps["physical_width"] / dx * 25.4, caps["physical_height"] / dy * 25.4]
        if not (abs(size_mm[0] - 210) < 2 and abs(size_mm[1] - 297) < 2):
            raise RuntimeError(f"Queue defaults are not portrait A4: {size_mm} mm. No job created.")
        report.update({"gdi_caps": caps, "paper_mm": size_mm, "copies": 1, "pages": 1,
                       "square_mm": [25.4, 25.4], "phase": "gdi-ready"})
        write_report(report)
        doc = DOCINFOW(C.sizeof(DOCINFOW), JOB_NAME, None, None, 0)
        job_id = positive(gdi.StartDocW(dc, C.byref(doc)), "StartDocW")
        started = True
        report.update({"job_id": job_id, "phase": "spooling", "job_started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()})
        write_report(report)
        print(json.dumps({"phase": "spooling", "job_id": job_id, "job_name": JOB_NAME, "queue": QUEUE}), flush=True)
        positive(gdi.StartPage(dc), "StartPage")
        font = gdi.CreateFontW(-round(14 * dy / 72), 0, 0, 0, 400,
                               0, 0, 0, 1, 0, 0, 0, 0, "Arial")
        if not font:
            fail("CreateFontW")
        old_font = gdi.SelectObject(dc, font)
        if not old_font or old_font == C.c_void_p(-1).value:
            fail("SelectObject(font)")
        positive(gdi.SetBkMode(dc, 1), "SetBkMode")  # TRANSPARENT
        if gdi.SetTextColor(dc, 0) == 0xFFFFFFFF:
            fail("SetTextColor")
        lines = [
            "HP LaserJet P1006 - Windows IPP queue test",
            "Проверка печати через очередь Windows. Русский текст.",
            "English text / 0123456789 / one page, one copy",
            "GDI -> Windows spooler -> Microsoft IPP Class Driver",
            f"Spooler job ID: {job_id}",
        ]
        x = round(dx * 0.5)
        for index, text in enumerate(lines):
            y = round(dy * (0.5 + index * 0.35))
            if not gdi.TextOutW(dc, x, y, text, len(text.encode("utf-16-le")) // 2):
                fail("TextOutW")
        old_brush = gdi.SelectObject(dc, gdi.GetStockObject(5))  # NULL_BRUSH
        if not old_brush or old_brush == C.c_void_p(-1).value:
            fail("SelectObject(NULL_BRUSH)")
        top = round(dy * 2.5)
        if not gdi.Rectangle(dc, x, top, x + dx, top + dy):
            fail("Rectangle")
        caption = "25.4 x 25.4 mm (1 x 1 inch)"
        if not gdi.TextOutW(dc, x, top + round(dy * 1.15), caption, len(caption)):
            fail("TextOutW(caption)")
        positive(gdi.EndPage(dc), "EndPage")
        positive(gdi.EndDoc(dc), "EndDoc")
        started = False
        report.update({"phase": "submitted-to-windows", "submitted": True,
                       "physical_output_confirmed": False,
                       "message": "EndDoc succeeded. Correlate this job with the bridge log and inspect the printed sheet."})
    finally:
        if started and dc:
            # Applies to rendering errors, EndDoc errors and Ctrl+C. A job may
            # already have reached the printer; never submit an automatic retry.
            report["abort_doc_result"] = gdi.AbortDoc(dc)
            report["partial_output_possible"] = True
        if dc:
            if old_brush and old_brush != C.c_void_p(-1).value:
                gdi.SelectObject(dc, old_brush)
            if old_font and old_font != C.c_void_p(-1).value:
                gdi.SelectObject(dc, old_font)
            if font:
                gdi.DeleteObject(font)
            gdi.DeleteDC(dc)
        if printer.value:
            spool.ClosePrinter(printer)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-only", action="store_true", help="Verify queue/driver and save metadata without printing.")
    arguments = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    report = {"queue": QUEUE, "expected_driver": DRIVER, "job_name": JOB_NAME,
              "inspect_only": arguments.inspect_only, "submitted": False,
              "physical_output_confirmed": False, "report": str(REPORT)}
    result = 0
    try:
        run(arguments.inspect_only, report)
    except (Exception, KeyboardInterrupt) as error:
        report.update({"failed_phase": report.get("phase"), "phase": "failed",
                       "error": str(error) or type(error).__name__})
        result = 1
    try:
        write_report(report)
    except OSError as error:
        report["report_write_error"] = str(error)
        result = 1
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
