"""Convert complete jobs before touching the printer; never retry print writes."""

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .firmware import ROOT, firmware_bytes, has_firmware
from .render import WIDTH, HEIGHT, file_pages, write_pbm
from .usb import UsbPrinter

STATE = ROOT / "state"
ENCODER = ROOT / "bin/foo2xqx.exe"


def encode_pages(pages, output: Path, copies: int = 1) -> dict:
    if not 1 <= copies <= 99:
        raise ValueError("copies must be 1..99")
    if not ENCODER.exists():
        raise FileNotFoundError("Build the converter first: powershell -File scripts/build.ps1")
    STATE.mkdir(exist_ok=True)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="encode-", dir=STATE) as folder:
        pbm = Path(folder) / "pages.pbm"
        count = 0
        with pbm.open("wb") as stream:
            for count, page in enumerate(pages, 1):
                if count > 100:
                    raise ValueError("A job may contain at most 100 pages")
                if page.size != (WIDTH, HEIGHT):
                    raise ValueError(f"Unexpected page size {page.size}")
                write_pbm(page, stream)
        if not count:
            raise ValueError("No pages to print")
        if count * copies > 1000:
            raise ValueError("A job may contain at most 1000 pages including copies")
        # XQXI_COPIES is per-page, so foo2xqx -nN produces uncollated copies.
        # IPP single-document requires collated output: repeat the whole PBM.
        if count > 1 and copies > 1:
            original_length = pbm.stat().st_size
            with pbm.open("rb") as source, pbm.open("ab") as target:
                for _ in range(copies - 1):
                    source.seek(0)
                    remaining = original_length
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RuntimeError("Unexpected end of PBM spool")
                        target.write(chunk)
                        remaining -= len(chunk)
        result_path = Path(folder) / "job.xqx"
        command = [str(ENCODER), "-r1200x600", f"-g{WIDTH}x{HEIGHT}", "-p9",
                   "-u176x84", "-l176x84", "-L3", "-s7", "-m1", "-d1", "-T3", f"-n{copies if count == 1 else 1}"]
        with pbm.open("rb") as source, result_path.open("wb") as target:
            result = subprocess.run(command, stdin=source, stdout=target, stderr=subprocess.PIPE,
                                    timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("foo2xqx failed: " + result.stderr.decode("utf-8", "replace"))
        data = result_path.read_bytes()
        if b",XQX" not in data[:4096]:
            raise ValueError("Encoder did not generate an XQX job")
        os.replace(result_path, output)
    return {"pages": count, "copies": copies, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "output": str(output), "resolution": [1200, 600], "media": "A4"}


@contextlib.contextmanager
def printer_lock():
    """Process-wide Windows byte lock shared by CLI and IPP worker."""
    import msvcrt
    STATE.mkdir(exist_ok=True)
    with (STATE / "printer.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Another P1006 process is using the printer") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def ready_printer(path: str | None = None):
    printer = UsbPrinter(path)
    printer.__enter__()
    try:
        identity = printer.get_device_id()
        if not has_firmware(identity):
            printer.write(firmware_bytes(), timeout=30)
            # Some printers re-enumerate after firmware. Reopen rather than
            # continuing on a stale handle. Only status requests are retried.
            printer.close()
            deadline = time.monotonic() + 30
            last_error = None
            while time.monotonic() < deadline:
                time.sleep(0.5)
                try:
                    printer = UsbPrinter(path)
                    printer.__enter__()
                    identity = printer.get_device_id(timeout=3)
                    if has_firmware(identity):
                        break
                    printer.close()
                except OSError as exc:
                    last_error = exc
                    printer.close()
            else:
                raise RuntimeError(f"Firmware sent but FWVER was not confirmed. No print job sent. {last_error or identity}")
        yield printer, identity
    finally:
        printer.close()


def submit_xqx(path: Path, device_path: str | None = None) -> dict:
    data = path.read_bytes()
    if b",XQX" not in data[:4096]:
        raise ValueError("Not an XQX print job")
    with printer_lock(), ready_printer(device_path) as (printer, identity):
        # USB port bits are diagnostic only; the hardware test must establish
        # whether this model reports reliable paper/error state.
        before = printer.get_status()
        sent = printer.write(data, timeout=180)
    result = {"submitted": True, "bytes_sent": sent, "device_id": identity,
              "status_before": before, "physical_output_confirmed": False,
              "message": "Data accepted by USB; check the printed paper."}
    STATE.mkdir(exist_ok=True)
    (STATE / "last-transfer.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def process_document(data: bytes, document_format: str, options: dict, *, dry_run: bool = False) -> dict:
    job_dir = Path(options["job-dir"])
    suffix = ".pdf" if document_format == "application/pdf" else ".pwg"
    source = job_dir / ("document" + suffix)
    source.write_bytes(data)
    output = job_dir / "print.xqx"
    resolution = tuple(options.get("printer-resolution", (1200, 600))[:2])
    result = encode_pages(file_pages(source, document_format, resolution=resolution),
                          output, copies=options.get("copies", 1))
    if dry_run:
        # IPP must not claim that a saved file was sent to hardware.
        return {**result, "submitted": False, "dry_run": True, "message": "Dry run: XQX saved; no USB transfer"}
    return {**result, **submit_xqx(output)}
