import argparse
import functools
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .firmware import ROOT, FIRMWARE_IMG, firmware_bytes
from .printing import STATE, ENCODER, encode_pages, printer_lock, ready_printer, submit_xqx, process_document
from .render import file_pages, test_page
from .usb import UsbPrinter, list_printers


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def diagnose() -> dict:
    result = {"version": __version__, "time_utc": datetime.now(timezone.utc).isoformat(),
              "os": platform.platform(), "process_arch": platform.machine(),
              "host_arch_env": os.environ.get("PROCESSOR_ARCHITECTURE"),
              "python": sys.executable, "encoder_exists": ENCODER.exists(),
              "firmware_exists": FIRMWARE_IMG.exists(), "hardware_print_verified": False}
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.IsWow64Process2.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(ctypes.c_uint16)]
        process_machine, native_machine = ctypes.c_uint16(), ctypes.c_uint16()
        if kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine)):
            machines = {0: "not reported by IsWow64Process2", 0x014C: "x86", 0x8664: "AMD64", 0xAA64: "ARM64"}
            result["native_arch"] = machines.get(native_machine.value, hex(native_machine.value))
            result["emulated_process_arch"] = machines.get(process_machine.value, hex(process_machine.value))
    try:
        devices = list_printers()
        result["devices"] = devices
        for device in devices:
            try:
                with UsbPrinter(device["path"], read_only=True) as printer:
                    device["device_id"] = printer.get_device_id()
                    device["status"] = printer.get_status()
            except OSError as exc:
                device["error"] = str(exc)
        result["next_step"] = "Run test-page after loading paper" if devices else "Connect and power on HP LaserJet P1006 over USB, then run diagnose again"
    except OSError as exc:
        result["discovery_error"] = str(exc)
    STATE.mkdir(exist_ok=True)
    (STATE / "diagnostics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="HP P1006 userspace printing bridge (Windows 11 ARM64)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("diagnose", help="Read-only USB discovery and status")
    fw = sub.add_parser("firmware", help="Load the P1006's volatile runtime firmware if needed")
    fw.add_argument("--output", type=Path, help="Only save .dl; no USB")
    fw.add_argument("--device-path")
    for name, help_text in [("test-page", "Print the generated A4 test page"), ("print", "Print PDF, PNG, PBM, or PWG raster")]:
        p = sub.add_parser(name, help=help_text)
        if name == "print":
            p.add_argument("file", type=Path)
        else:
            p.add_argument("--hp-demo", action="store_true", help="Use the unmodified HP demo (Letter layout)")
        p.add_argument("--output", type=Path, help="Only save .xqx; no USB")
        p.add_argument("--copies", type=int, default=1)
        p.add_argument("--device-path")
    server = sub.add_parser("serve", help="Run the local IPP bridge; Ctrl+C stops it")
    server.add_argument("--port", type=int, default=8631)
    server.add_argument("--dry-run", action="store_true", help="Save jobs but never transmit USB")
    server.add_argument("--install-queue", action="store_true", help="Create/check the Windows IPP queue after starting the bridge")
    args = parser.parse_args()
    STATE.mkdir(exist_ok=True)
    if args.command == "diagnose":
        emit(diagnose())
    elif args.command == "firmware":
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(firmware_bytes())
            emit({"output": str(args.output), "usb_sent": False})
        else:
            with printer_lock(), ready_printer(args.device_path) as (_, identity):
                emit({"device_id": identity, "firmware_ready": True})
    elif args.command in ("test-page", "print"):
        output = args.output or STATE / ("job-" + uuid.uuid4().hex + ".xqx")
        if args.command == "test-page" and args.hp_demo:
            if args.copies != 1:
                raise ValueError("The original HP demo is one copy only")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes((ROOT / ".vendor/hp/driver/P1006DP.PRN").read_bytes())
            result = {"source": "HP P1006DP.PRN", "output": str(output), "media": "Letter"}
        else:
            pages = [test_page()] if args.command == "test-page" else file_pages(args.file)
            result = encode_pages(pages, output, args.copies)
        if not args.output:
            result.update(submit_xqx(output, args.device_path))
        else:
            result["usb_sent"] = False
        emit(result)
    elif args.command == "serve":
        from .ipp import create_server
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.FileHandler(STATE / "bridge.log", encoding="utf-8"),
                      logging.StreamHandler()],
        )
        server = create_server(functools.partial(process_document, dry_run=args.dry_run), STATE / "ipp", port=args.port)
        print(f"P1006 IPP: http://127.0.0.1:{args.port}/ipp/print; dry_run={args.dry_run}", flush=True)
        serving = threading.Thread(target=server.serve_forever, name="p1006-http")
        serving.start()
        try:
            if args.install_queue:
                powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
                command = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(ROOT / "scripts/install-printer.ps1"), "-Port", str(args.port)]
                try:
                    installed = subprocess.run(command, capture_output=True, timeout=180,
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    for output in (installed.stdout, installed.stderr):
                        if output:
                            print(output.decode("utf-8", "replace"), flush=True)
                    if installed.returncode:
                        logging.error("Windows queue setup failed (exit %s). Bridge remains running; see state/windows-printer.json.", installed.returncode)
                except subprocess.TimeoutExpired:
                    logging.error("Queue setup timed out; its final state is unknown. Bridge remains running. Check Windows printers before retrying.")
            print("Leave this window open while printing. Ctrl+C stops the bridge.", flush=True)
            while serving.is_alive():
                serving.join(0.5)
        finally:
            server.shutdown()
            serving.join()
            server.server_close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped. An interrupted USB job may have been partially printed.", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
