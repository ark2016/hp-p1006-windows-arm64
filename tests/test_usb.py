"""Offline safety and transport regression tests; no real printer is opened."""

import ctypes as C
import types
import unittest
from unittest.mock import patch

from p1006 import usb


class FakeKernel:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.writes = []
        self.closed = []
        self.count = 0

    def CreateEventW(self, *_):
        return 321

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True

    def WriteFile(self, handle, buffer, size, unused_count, operation):
        data = C.string_at(buffer, size)
        self.count = self.results.pop(0) if self.results else size
        self.writes.append(data[:self.count])
        return True

    def GetOverlappedResult(self, handle, operation, count, wait):
        C.cast(count, C.POINTER(usb.DWORD)).contents.value = self.count
        return True

    def DeviceIoControl(self, handle, code, in_buffer, in_size, buffer, size, count, operation):
        data = b"\x18\0\0\0" if code == usb.IOCTL_USBPRINT_GET_LPT_STATUS else b"\0\x26MFG:HP;MDL:HP LaserJet P1006;\0"
        C.memmove(buffer, data, len(data))
        self.count = len(data)
        return True


def fake_printer(kernel, *, read_only=False):
    printer = usb.UsbPrinter(read_only=read_only)
    printer._api = types.SimpleNamespace(kernel=kernel)
    printer._handle = 123
    return printer


class UsbTests(unittest.TestCase):
    def test_native_layout(self):
        self.assertEqual(C.sizeof(usb.GUID), 16)
        self.assertEqual(C.sizeof(usb.DWORD), 4)
        self.assertEqual(C.sizeof(usb.SP_DEVICE_INTERFACE_DATA), 32 if C.sizeof(C.c_void_p) == 8 else 28)
        self.assertEqual(C.sizeof(usb.SP_DEVINFO_DATA), 32 if C.sizeof(C.c_void_p) == 8 else 28)
        self.assertEqual(C.sizeof(usb.OVERLAPPED), 32 if C.sizeof(C.c_void_p) == 8 else 20)

    def test_model_selection_requires_exact_usb_id(self):
        self.assertTrue(usb._is_p1006([r"USB\VID_03F0&PID_3E17&REV_0100"]))
        self.assertTrue(usb._is_p1006([r"usb\vid_03f0&pid_3e17\SERIAL"]))
        for wrong in [r"USB\VID_03F0&PID_3E170", r"USB\VID_03F0&PID_3E18", "HP LaserJet P1006", r"FAKE\USB\VID_03F0&PID_3E17"]:
            self.assertFalse(usb._is_p1006([wrong]), wrong)

    def test_setupapi_enumeration_uses_pnp_identity_and_opaque_path(self):
        state = {"error": 0, "index": 0, "destroyed": False}
        paths = [r"\\?\opaque-interface-one", r"\\?\opaque-interface-two"]

        def enum(info, device, guid, index, interface):
            state["index"] = index
            if index == len(paths):
                state["error"] = usb.ERROR_NO_MORE_ITEMS
                return False
            return True

        def detail(info, interface, buffer, size, required, device):
            data = paths[state["index"]].encode("utf-16-le") + b"\0\0"
            if not buffer:
                C.cast(required, C.POINTER(usb.DWORD)).contents.value = len(data) + 4
                state["error"] = usb.ERROR_INSUFFICIENT_BUFFER
                return False
            expected_size = 8 if C.sizeof(C.c_void_p) == 8 else 6
            self.assertEqual(usb.DWORD.from_buffer(buffer).value, expected_size)
            C.memmove(C.addressof(buffer) + 4, data, len(data))
            C.cast(device, C.POINTER(usb.SP_DEVINFO_DATA)).contents.DevInst = state["index"] + 10
            return True

        def prop(api, info, device, code):
            if code == 1:
                return [r"USB\VID_03F0&PID_3E17"] if device.DevInst == 10 else [r"USB\VID_03F0&PID_1234"]
            return ["Test printer"]

        setup = types.SimpleNamespace(
            SetupDiGetClassDevsW=lambda *args: 999,
            SetupDiEnumDeviceInterfaces=enum,
            SetupDiGetDeviceInterfaceDetailW=detail,
            SetupDiDestroyDeviceInfoList=lambda handle: state.update(destroyed=True),
        )
        with patch.object(usb, "_WinApi", return_value=types.SimpleNamespace(setup=setup)), patch.object(usb, "_registry_property", side_effect=prop), patch.object(usb, "_device_ids", return_value=[]), patch.object(C, "get_last_error", side_effect=lambda: state["error"], create=True):
            result = usb.list_printers()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], paths[0])
        self.assertTrue(state["destroyed"])

    def test_open_requires_one_device_and_rejects_arbitrary_path(self):
        for devices in [[], [{"path": "one"}, {"path": "two"}]]:
            with patch.object(usb, "list_printers", return_value=devices), patch.object(usb, "_WinApi") as api:
                with self.assertRaises(usb.UsbPrinterError):
                    usb.UsbPrinter().__enter__()
                api.assert_not_called()
        with patch.object(usb, "list_printers", return_value=[{"path": "known"}]), patch.object(usb, "_WinApi") as api:
            with self.assertRaises(usb.UsbPrinterError):
                usb.UsbPrinter("C:\\unrelated-file").__enter__()
            api.assert_not_called()

    def test_explicit_path_resolves_multiple_and_close(self):
        kernel = FakeKernel()
        kernel.CreateFileW = lambda *args: 123
        with patch.object(usb, "list_printers", return_value=[{"path": "one"}, {"path": "two"}]), patch.object(usb, "_WinApi", return_value=types.SimpleNamespace(kernel=kernel)):
            with usb.UsbPrinter("TWO") as printer:
                self.assertEqual(printer.path, "TWO")
            self.assertIn(123, kernel.closed)

    def test_chunk_and_short_writes_preserve_exact_byte_stream(self):
        kernel = FakeKernel(results=[3, 2, 4, 1])
        printer = fake_printer(kernel)
        self.assertEqual(printer.write(b"0123456789", chunk_size=4), 10)
        self.assertEqual(b"".join(kernel.writes), b"0123456789")
        self.assertEqual(len(kernel.closed), 4)

    def test_read_only_forbids_writes_but_allows_queries(self):
        kernel = FakeKernel()
        printer = fake_printer(kernel, read_only=True)
        with self.assertRaises(usb.UsbPrinterError):
            printer.write(b"print data")
        self.assertEqual(kernel.writes, [])
        self.assertIn("P1006", printer.get_device_id())
        self.assertEqual(printer.get_status(), {"raw": 24, "paper_empty": False, "selected": True, "not_error": True})

    def test_zero_byte_write_is_error_without_retry(self):
        kernel = FakeKernel(results=[0])
        printer = fake_printer(kernel)
        with self.assertRaises(usb.UsbPrinterError) as caught:
            printer.write(b"test")
        self.assertEqual(caught.exception.bytes_sent, 0)
        self.assertEqual(len(kernel.writes), 1)

    def test_empty_write_sends_nothing(self):
        kernel = FakeKernel()
        self.assertEqual(fake_printer(kernel).write(b""), 0)
        self.assertEqual(kernel.writes, [])

    def test_invalid_timeout_and_chunk_fail_before_io(self):
        kernel = FakeKernel()
        printer = fake_printer(kernel)
        for timeout in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                printer.write(b"test", timeout=timeout)
        for size in [0, -1, 1.5, 1048577]:
            with self.assertRaises(ValueError):
                printer.write(b"test", chunk_size=size)
        self.assertEqual(kernel.writes, [])

    def test_status_error_bits(self):
        self.assertEqual(usb._decode_status(b"\x20\0\0\0"),
                         {"raw": 32, "paper_empty": True, "selected": False, "not_error": False})

    def test_timeout_cancels_and_retires_connection_no_retry(self):
        kernel = FakeKernel()
        kernel.WriteFile = lambda *args: False
        kernel.WaitForSingleObject = lambda *args: usb.WAIT_TIMEOUT
        retired = []
        printer = fake_printer(kernel)

        def retire(operation, buffer):
            retired.append((operation, buffer))
            printer._handle = None

        with patch.object(C, "get_last_error", return_value=usb.ERROR_IO_PENDING, create=True), patch.object(printer, "_cancel_and_retire", side_effect=retire):
            with self.assertRaises(usb.UsbTimeoutError) as caught:
                printer.write(b"data", timeout=0.01)
        self.assertEqual(caught.exception.bytes_sent, 0)
        self.assertEqual(len(retired), 1)
        self.assertEqual(kernel.closed, [])  # Reaper owns event and buffer now.
        with self.assertRaises(usb.UsbPrinterError):
            printer.write(b"retry")


if __name__ == "__main__":
    unittest.main()
