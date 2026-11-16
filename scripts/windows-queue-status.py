"""Read the named P1006 queue through Winspool, without CIM or printing.

Optional --cancel-job ID or --restart-job ID only affects the exact verification
document after a fresh GetJob check. Microsoft prescribes DELETE for cancellation:
https://learn.microsoft.com/windows/win32/printdocs/setjob
https://learn.microsoft.com/windows/win32/printdocs/enumjobs
https://learn.microsoft.com/windows/win32/printdocs/job-info-1
"""

from __future__ import annotations

import argparse
import ctypes as C
import datetime as dt
import json
import os
import sys

QUEUE = "HP LaserJet P1006 (ARM bridge)"
TEST_DOCUMENT = "P1006 Windows queue verification"
DWORD, WORD, BOOL, HANDLE = C.c_uint32, C.c_uint16, C.c_int32, C.c_void_p
MAX_BUFFER = 64 * 1024 * 1024
ERROR_INSUFFICIENT_BUFFER = 122


class SYSTEMTIME(C.Structure):
    _fields_ = [(name, WORD) for name in (
        "year", "month", "day_of_week", "day", "hour", "minute", "second", "milliseconds")]


class JOB_INFO_1W(C.Structure):
    _fields_ = [("JobId", DWORD), ("pPrinterName", C.c_wchar_p),
                ("pMachineName", C.c_wchar_p), ("pUserName", C.c_wchar_p),
                ("pDocument", C.c_wchar_p), ("pDatatype", C.c_wchar_p),
                ("pStatus", C.c_wchar_p), ("Status", DWORD), ("Priority", DWORD),
                ("Position", DWORD), ("TotalPages", DWORD), ("PagesPrinted", DWORD),
                ("Submitted", SYSTEMTIME)]


STATUS_BITS = {
    0x0001: "paused", 0x0002: "error", 0x0004: "deleting", 0x0008: "spooling",
    0x0010: "printing", 0x0020: "offline", 0x0040: "paper-out", 0x0080: "printed",
    0x0100: "deleted", 0x0200: "blocked-device-queue", 0x0400: "user-intervention",
    0x0800: "restart", 0x1000: "complete", 0x2000: "retained", 0x4000: "rendering-locally",
}


def fail(operation):
    code = C.get_last_error()
    raise OSError(code, f"{operation}: {C.FormatError(code).strip()}")


def api():
    if os.name != "nt":
        raise RuntimeError("Windows is required.")
    spool = C.WinDLL("winspool.drv", use_last_error=True)
    for name, result, args in [
        ("OpenPrinterW", BOOL, [C.c_wchar_p, C.POINTER(HANDLE), C.c_void_p]),
        ("ClosePrinter", BOOL, [HANDLE]),
        ("EnumJobsW", BOOL, [HANDLE, DWORD, DWORD, DWORD, C.c_void_p, DWORD,
                             C.POINTER(DWORD), C.POINTER(DWORD)]),
        ("GetJobW", BOOL, [HANDLE, DWORD, DWORD, C.c_void_p, DWORD, C.POINTER(DWORD)]),
        ("SetJobW", BOOL, [HANDLE, DWORD, DWORD, C.c_void_p, DWORD]),
    ]:
        function = getattr(spool, name)
        function.restype, function.argtypes = result, args
    return spool


def metadata(job):
    submitted = job.Submitted
    try:
        timestamp = dt.datetime(submitted.year, submitted.month, submitted.day,
                                submitted.hour, submitted.minute, submitted.second,
                                submitted.milliseconds * 1000, tzinfo=dt.timezone.utc).isoformat()
    except ValueError:
        timestamp = None
    return {"job_id": job.JobId, "queue": job.pPrinterName, "document": job.pDocument,
            "datatype": job.pDatatype, "status_text": job.pStatus,
            "status": job.Status, "status_hex": f"0x{job.Status:08x}",
            "status_names": [name for bit, name in STATUS_BITS.items() if job.Status & bit],
            "position": job.Position, "priority": job.Priority,
            "total_pages": job.TotalPages, "pages_printed": job.PagesPrinted,
            "submitted_at_utc": timestamp, "user": job.pUserName,
            "machine": job.pMachineName}


def enumerate_jobs(spool, handle):
    size = 0
    buffer = None
    for _ in range(4):
        needed, count = DWORD(), DWORD()
        if spool.EnumJobsW(handle, 0, 0xFFFFFFFF, 1, buffer, size, C.byref(needed), C.byref(count)):
            if count.value == 0:
                return []
            if buffer is None or count.value * C.sizeof(JOB_INFO_1W) > size:
                raise RuntimeError("EnumJobsW returned an invalid job count.")
            jobs = C.cast(buffer, C.POINTER(JOB_INFO_1W))
            return [metadata(jobs[index]) for index in range(count.value)]
        if C.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
            fail("EnumJobsW")
        size = needed.value
        if not 0 < size <= MAX_BUFFER:
            raise RuntimeError(f"Unexpected spooler buffer size: {size}")
        buffer = C.create_string_buffer(size)
    raise RuntimeError("Print queue changed too quickly to read a stable snapshot.")


def current_job(spool, handle, job_id):
    size = 0
    buffer = None
    for _ in range(4):
        needed = DWORD()
        if spool.GetJobW(handle, job_id, 1, buffer, size, C.byref(needed)):
            if buffer is None or size < C.sizeof(JOB_INFO_1W):
                raise RuntimeError("GetJobW returned an invalid buffer.")
            return metadata(C.cast(buffer, C.POINTER(JOB_INFO_1W)).contents)
        if C.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
            fail("GetJobW")
        size = needed.value
        if not C.sizeof(JOB_INFO_1W) <= size <= MAX_BUFFER:
            raise RuntimeError(f"Unexpected job buffer size: {size}")
        buffer = C.create_string_buffer(size)
    raise RuntimeError("Print job changed too quickly to read safely.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--cancel-job", type=int, metavar="ID",
                        help="Cancel only a job whose document is exactly the P1006 verification title.")
    action.add_argument("--restart-job", type=int, metavar="ID",
                        help="Restart only an existing job with the exact P1006 verification title.")
    args = parser.parse_args()
    selected_id = args.cancel_job if args.cancel_job is not None else args.restart_job
    if selected_id is not None and not 1 <= selected_id <= 0xFFFFFFFF:
        parser.error("job ID must be a positive Windows DWORD")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = {"queue": QUEUE, "observed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "read_only": selected_id is None, "physical_output_confirmed": False}
    handle = HANDLE()
    spool = None
    try:
        spool = api()
        if not spool.OpenPrinterW(QUEUE, C.byref(handle), None):
            fail("OpenPrinterW")
        if selected_id is not None:
            job = current_job(spool, handle, selected_id)
            if job["document"] != TEST_DOCUMENT or job["queue"] != QUEUE or job["job_id"] != selected_id:
                raise RuntimeError("Refusing job control: identity does not match the exact verification queue/document.")
            # JOB_CONTROL_CANCEL is documented as obsolete; DELETE is the
            # supported cancellation operation. Never purge the whole queue.
            command = 5 if args.cancel_job is not None else 4
            operation = "DELETE" if args.cancel_job is not None else "RESTART"
            if not spool.SetJobW(handle, selected_id, 0, None, command):
                fail(f"SetJobW(JOB_CONTROL_{operation})")
            result["cancel_requested" if args.cancel_job is not None else "restart_requested"] = job
        result["jobs"] = enumerate_jobs(spool, handle)
        result["job_count"] = len(result["jobs"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        result["error"] = str(error)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    finally:
        if spool is not None and handle.value:
            spool.ClosePrinter(handle)


if __name__ == "__main__":
    raise SystemExit(main())
