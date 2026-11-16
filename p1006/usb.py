"""HP LaserJet P1006 transport using the Windows inbox USBPRINT driver.

No custom kernel driver, libusb replacement, or driver installation is performed.
``list_printers`` only reads present PnP interfaces. Opening a ``UsbPrinter`` does
not send print data; only ``write`` does that. Writes use one total deadline and
are never retried automatically after a timeout (a partial job may have arrived).

Microsoft API references:
https://learn.microsoft.com/windows/win32/api/setupapi/nf-setupapi-setupdigetdeviceinterfacedetailw
https://learn.microsoft.com/windows-hardware/drivers/ddi/usbprint/ni-usbprint-ioctl_usbprint_get_1284_id
https://learn.microsoft.com/windows-hardware/drivers/ddi/usbprint/ni-usbprint-ioctl_usbprint_get_lpt_status
https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-writefile
https://learn.microsoft.com/windows/win32/api/ioapiset/nf-ioapiset-cancelioex

The ctypes definitions use Windows' fixed-width DWORD and pointer-width fields;
they cover native Windows ARM64, AMD64, and x86 Python. Hardware verification on
the target ARM64 computer and a connected P1006 is still required.
"""

from __future__ import annotations

import ctypes as C
import math
import os
import re
import threading
import time
import uuid
from typing import Callable

DWORD = C.c_uint32
BOOL = C.c_int32
HANDLE = C.c_void_p
ULONG_PTR = C.c_size_t
PDWORD = C.POINTER(DWORD)
INVALID_HANDLE_VALUE = C.c_void_p(-1).value
GUID_USBPRINT = "28d78fad-5a12-11d1-ae5b-0000f803a8c2"
IOCTL_USBPRINT_GET_LPT_STATUS = (0x22 << 16) | (12 << 2)
IOCTL_USBPRINT_GET_1284_ID = (0x22 << 16) | (13 << 2)
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_MORE_ITEMS = 259
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF
_P1006_ID = re.compile(r"^USB\\VID_03F0&PID_3E17(?:&[^\\]*)?(?:\\|$)", re.I)


class GUID(C.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", C.c_uint16),
                ("Data3", C.c_uint16), ("Data4", C.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(C.Structure):
    _fields_ = [("cbSize", DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", DWORD), ("Reserved", ULONG_PTR)]


class SP_DEVINFO_DATA(C.Structure):
    _fields_ = [("cbSize", DWORD), ("ClassGuid", GUID),
                ("DevInst", DWORD), ("Reserved", ULONG_PTR)]


class OVERLAPPED(C.Structure):
    # The offset pair occupies the same space as the Windows union's pointer.
    _fields_ = [("Internal", ULONG_PTR), ("InternalHigh", ULONG_PTR),
                ("Offset", DWORD), ("OffsetHigh", DWORD), ("hEvent", HANDLE)]


class UsbPrinterError(OSError):
    """Discovery, open, or transport failure; no automatic print-job retry."""


class UsbTimeoutError(TimeoutError):
    """The printer might have received some or all of the interrupted chunk."""


def _winerror(operation: str, code: int | None = None) -> UsbPrinterError:
    code = C.get_last_error() if code is None else code
    return UsbPrinterError(code, f"{operation}: {C.FormatError(code).strip()}")


class _WinApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise UsbPrinterError("USBPRINT transport requires Windows.")
        self.setup = C.WinDLL("setupapi", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.cfg = C.WinDLL("cfgmgr32", use_last_error=True)
        definitions = [
            (self.setup, "SetupDiGetClassDevsW", HANDLE, [C.POINTER(GUID), C.c_wchar_p, HANDLE, DWORD]),
            (self.setup, "SetupDiEnumDeviceInterfaces", BOOL,
             [HANDLE, C.c_void_p, C.POINTER(GUID), DWORD, C.POINTER(SP_DEVICE_INTERFACE_DATA)]),
            (self.setup, "SetupDiGetDeviceInterfaceDetailW", BOOL,
             [HANDLE, C.POINTER(SP_DEVICE_INTERFACE_DATA), C.c_void_p, DWORD, PDWORD, C.POINTER(SP_DEVINFO_DATA)]),
            (self.setup, "SetupDiGetDeviceRegistryPropertyW", BOOL,
             [HANDLE, C.POINTER(SP_DEVINFO_DATA), DWORD, PDWORD, C.c_void_p, DWORD, PDWORD]),
            (self.setup, "SetupDiDestroyDeviceInfoList", BOOL, [HANDLE]),
            (self.cfg, "CM_Get_Parent", DWORD, [PDWORD, DWORD, DWORD]),
            (self.cfg, "CM_Get_Device_IDW", DWORD, [DWORD, C.c_wchar_p, DWORD, DWORD]),
            (self.kernel, "CreateFileW", HANDLE,
             [C.c_wchar_p, DWORD, DWORD, C.c_void_p, DWORD, DWORD, HANDLE]),
            (self.kernel, "CreateEventW", HANDLE, [C.c_void_p, BOOL, BOOL, C.c_wchar_p]),
            (self.kernel, "CloseHandle", BOOL, [HANDLE]),
            (self.kernel, "WriteFile", BOOL,
             [HANDLE, C.c_void_p, DWORD, PDWORD, C.POINTER(OVERLAPPED)]),
            (self.kernel, "DeviceIoControl", BOOL,
             [HANDLE, DWORD, C.c_void_p, DWORD, C.c_void_p, DWORD, PDWORD, C.POINTER(OVERLAPPED)]),
            (self.kernel, "GetOverlappedResult", BOOL,
             [HANDLE, C.POINTER(OVERLAPPED), PDWORD, BOOL]),
            (self.kernel, "CancelIoEx", BOOL, [HANDLE, C.POINTER(OVERLAPPED)]),
            (self.kernel, "WaitForSingleObject", DWORD, [HANDLE, DWORD]),
        ]
        for library, name, result, args in definitions:
            function = getattr(library, name)
            function.restype, function.argtypes = result, args


def _registry_property(api: _WinApi, info: int, device: SP_DEVINFO_DATA, prop: int) -> list[str]:
    size, kind = DWORD(), DWORD()
    success = api.setup.SetupDiGetDeviceRegistryPropertyW(
        info, C.byref(device), prop, C.byref(kind), None, 0, C.byref(size))
    if not success and C.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
        return []  # Optional property can be missing; identity then fails closed.
    if not size.value:
        return []
    buffer = C.create_string_buffer(size.value)
    if not api.setup.SetupDiGetDeviceRegistryPropertyW(
            info, C.byref(device), prop, C.byref(kind), buffer, size.value, C.byref(size)):
        return []
    return [part for part in buffer.raw.decode("utf-16-le", errors="replace").split("\0") if part]


def _device_ids(api: _WinApi, devinst: int) -> list[str]:
    ids = []
    for _ in range(4):
        buffer = C.create_unicode_buffer(1024)
        if api.cfg.CM_Get_Device_IDW(devinst, buffer, len(buffer), 0) == 0:
            ids.append(buffer.value)
        parent = DWORD()
        if api.cfg.CM_Get_Parent(C.byref(parent), devinst, 0) != 0:
            break
        devinst = parent.value
    return ids


def _is_p1006(ids: list[str]) -> bool:
    return any(_P1006_ID.match(identifier) for identifier in ids)


def list_printers() -> list[dict]:
    """List present USBPRINT interfaces with the P1006's exact USB VID/PID.

    Reads PnP properties only: no device is opened and no USB requests are sent.
    Interface paths are opaque; model selection uses hardware and devnode IDs.
    """
    api = _WinApi()
    guid = GUID.from_buffer_copy(uuid.UUID(GUID_USBPRINT).bytes_le)
    info = api.setup.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x12)
    if info == INVALID_HANDLE_VALUE:
        raise _winerror("SetupDiGetClassDevsW")
    found = []
    try:
        index = 0
        while True:
            interface = SP_DEVICE_INTERFACE_DATA()
            interface.cbSize = C.sizeof(interface)
            if not api.setup.SetupDiEnumDeviceInterfaces(info, None, C.byref(guid), index, C.byref(interface)):
                if C.get_last_error() == ERROR_NO_MORE_ITEMS:
                    break
                raise _winerror("SetupDiEnumDeviceInterfaces")
            index += 1
            size = DWORD()
            api.setup.SetupDiGetDeviceInterfaceDetailW(info, C.byref(interface), None, 0, C.byref(size), None)
            if C.get_last_error() != ERROR_INSUFFICIENT_BUFFER or size.value < 6:
                raise _winerror("SetupDiGetDeviceInterfaceDetailW(size)")
            detail = C.create_string_buffer(size.value)
            # Windows setupapi.h packs the x86 W structure to 6 bytes; 64-bit
            # (including ARM64) rounds its size to 8. DevicePath is at offset 4.
            DWORD.from_buffer(detail).value = 8 if C.sizeof(HANDLE) == 8 else 6
            device = SP_DEVINFO_DATA()
            device.cbSize = C.sizeof(device)
            if not api.setup.SetupDiGetDeviceInterfaceDetailW(
                    info, C.byref(interface), detail, size.value, None, C.byref(device)):
                raise _winerror("SetupDiGetDeviceInterfaceDetailW")
            hardware_ids = _registry_property(api, info, device, 1)  # SPDRP_HARDWAREID
            instance_ids = _device_ids(api, device.DevInst)
            if not _is_p1006(hardware_ids + instance_ids):
                continue
            names = _registry_property(api, info, device, 12)  # SPDRP_FRIENDLYNAME
            if not names:
                names = _registry_property(api, info, device, 0)  # SPDRP_DEVICEDESC
            path = detail.raw[4:].decode("utf-16-le").split("\0", 1)[0]
            found.append({"path": path, "description": names[0] if names else "HP LaserJet P1006",
                          "hardware_ids": hardware_ids, "instance_ids": instance_ids,
                          "vid": "03f0", "pid": "3e17"})
    finally:
        api.setup.SetupDiDestroyDeviceInfoList(info)
    return found


def _decode_device_id(data: bytes) -> str:
    if len(data) < 2:
        raise UsbPrinterError("USBPRINT returned an incomplete IEEE 1284 device ID.")
    # Keep the returned payload: printers disagree whether the length includes
    # the two prefix bytes. The actual returned count and NUL bound the string.
    result = data[2:].split(b"\0", 1)[0].decode("ascii", errors="replace").strip()
    if not result:
        raise UsbPrinterError("USBPRINT returned an empty IEEE 1284 device ID.")
    return result


def _decode_status(data: bytes) -> dict:
    if len(data) < 1:
        raise UsbPrinterError("USBPRINT returned an empty port status.")
    value = int.from_bytes(data, "little")
    return {"raw": value, "paper_empty": bool(value & 0x20),
            "selected": bool(value & 0x10), "not_error": bool(value & 0x08)}


def _check_timeout(timeout: float) -> float:
    value = float(timeout)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive finite number of seconds")
    return value


class UsbPrinter:
    """Exclusive direct connection to one validated, present P1006 interface.

    Use as a context manager. A supplied path must match the current discovery
    result; an arbitrary file/device path is never opened. With several P1006s,
    an explicit path is required. ``read_only=True`` prevents all print writes.
    Opening may fail if the Windows spooler has the USB port in use. The caller
    must resolve that contention; this class never stops services or jobs.
    """

    def __init__(self, path: str | None = None, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        self._api = None
        self._handle = None
        self._lock = threading.Lock()

    def __enter__(self) -> "UsbPrinter":
        with self._lock:
            if self._handle is not None:
                raise UsbPrinterError("The printer connection is already open.")
            printers = list_printers()
            if self.path is None:
                if len(printers) != 1:
                    raise UsbPrinterError(
                        f"Expected exactly one HP LaserJet P1006; found {len(printers)}. "
                        "Connect the printer, or select its explicit path if there are several.")
                self.path = printers[0]["path"]
            elif not any(item["path"].casefold() == self.path.casefold() for item in printers):
                raise UsbPrinterError("The selected path is not a present HP LaserJet P1006 USBPRINT interface.")
            self._api = _WinApi()
            access = 0x80000000 if self.read_only else 0xC0000000
            self._handle = self._api.kernel.CreateFileW(self.path, access, 0, None, 3, 0x40000000, None)
            if self._handle == INVALID_HANDLE_VALUE:
                self._handle = None
                raise _winerror("Open HP LaserJet P1006 (USB port may be busy)")
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._api.kernel.CloseHandle(self._handle)
                self._handle = None

    def _require_open(self) -> None:
        if self._handle is None:
            raise UsbPrinterError("Printer is not open; use 'with UsbPrinter(...) as printer:'.")

    def _cancel_and_retire(self, operation: OVERLAPPED, buffer) -> None:
        # CancelIoEx is a request, not completion. Keep every pointer and both
        # handles alive until the I/O has actually completed. A stuck driver
        # cannot hold the caller forever: the cleanup thread is a daemon.
        kernel, handle = self._api.kernel, self._handle
        kernel.CancelIoEx(handle, C.byref(operation))
        self._handle = None

        def reap() -> None:
            keep_buffer_alive = buffer
            count = DWORD()
            kernel.GetOverlappedResult(handle, C.byref(operation), C.byref(count), True)
            kernel.CloseHandle(operation.hEvent)
            kernel.CloseHandle(handle)
            del keep_buffer_alive

        threading.Thread(target=reap, name="p1006-cancel-io", daemon=True).start()

    def _transfer(self, start: Callable, buffer, timeout: float) -> int:
        self._require_open()
        kernel = self._api.kernel
        operation = OVERLAPPED()
        operation.hEvent = kernel.CreateEventW(None, True, False, None)
        if not operation.hEvent:
            raise _winerror("CreateEventW")
        retired = False
        pending = False
        try:
            # Mark it before entering ctypes: Ctrl+C can be delivered as soon
            # as WriteFile returns, before its result has been inspected.
            pending = True
            if not start(C.byref(operation)):
                code = C.get_last_error()
                if code != ERROR_IO_PENDING:
                    pending = False
                    raise _winerror("USB transfer", code)
                wait_ms = min(0xFFFFFFFE, max(1, math.ceil(timeout * 1000)))
                result = kernel.WaitForSingleObject(operation.hEvent, wait_ms)
                if result != WAIT_OBJECT_0:
                    code = C.get_last_error()
                    self._cancel_and_retire(operation, buffer)
                    retired = True
                    pending = False
                    if result == WAIT_TIMEOUT:
                        raise UsbTimeoutError(
                            "USB transfer timed out; connection closed. Some bytes may have arrived. "
                            "Do not automatically retry the job.")
                    raise _winerror("WaitForSingleObject", code)
            pending = False
            count = DWORD()
            if not kernel.GetOverlappedResult(self._handle, C.byref(operation), C.byref(count), False):
                raise _winerror("GetOverlappedResult")
            return count.value
        finally:
            if pending:  # Also retain pending buffers if interrupted by Ctrl+C.
                self._cancel_and_retire(operation, buffer)
                retired = True
            if not retired:
                kernel.CloseHandle(operation.hEvent)

    def _ioctl(self, code: int, size: int, timeout: float) -> bytes:
        timeout = _check_timeout(timeout)
        with self._lock:
            self._require_open()
            buffer = C.create_string_buffer(size)
            count = self._transfer(
                lambda operation: self._api.kernel.DeviceIoControl(
                    self._handle, code, None, 0, buffer, size, None, operation), buffer, timeout)
            if count > size:
                raise UsbPrinterError("USBPRINT returned an invalid output byte count.")
            return buffer.raw[:count]

    def get_device_id(self, timeout: float = 10.0) -> str:
        """Read the IEEE 1284 ID; uses 4094 bytes to avoid documented USB quirks."""
        return _decode_device_id(self._ioctl(IOCTL_USBPRINT_GET_1284_ID, 4094, timeout))

    def get_status(self, timeout: float = 10.0) -> dict:
        """Read the USB printer class status bits (not a print-completion signal)."""
        return _decode_status(self._ioctl(IOCTL_USBPRINT_GET_LPT_STATUS, 4, timeout))

    def write(self, data: bytes, timeout: float = 120.0, *, chunk_size: int = 65536) -> int:
        """Send all bytes with one total deadline; return confirmed bytes sent.

        Accepts bytes-like data. Short writes continue from the confirmed offset.
        A timeout retires the handle and reports ``bytes_sent`` for completed
        chunks only; the timed-out chunk's delivery is unknown. This says nothing
        about whether paper has physically printed.
        """
        timeout = _check_timeout(timeout)
        if not isinstance(chunk_size, int) or not 1 <= chunk_size <= 1048576:
            raise ValueError("chunk_size must be between 1 and 1048576 bytes")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        payload = bytes(data)
        with self._lock:
            self._require_open()
            if self.read_only:
                raise UsbPrinterError("This connection is read-only.")
            sent = 0
            deadline = time.monotonic() + timeout
            try:
                while sent < len(payload):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._api.kernel.CloseHandle(self._handle)
                        self._handle = None
                        raise UsbTimeoutError("Total USB write deadline exceeded; partial job may have arrived.")
                    piece = payload[sent:sent + chunk_size]
                    buffer = C.create_string_buffer(piece, len(piece))
                    count = self._transfer(
                        lambda operation: self._api.kernel.WriteFile(
                            self._handle, buffer, len(piece), None, operation), buffer, remaining)
                    if not 0 < count <= len(piece):
                        raise UsbPrinterError("WriteFile returned an invalid or zero byte count.")
                    sent += count
            except (OSError, TimeoutError) as error:
                error.bytes_sent = sent
                raise
            return sent
