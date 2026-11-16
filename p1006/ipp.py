"""Loopback IPP-to-USB bridge, a practical prototype, not certified IPP Everywhere.

Wire encoding: RFC 8010, model/status codes: RFC 8011. The single worker calls
process_document(data, mime_type, options). A result must contain submitted=True
after the entire job has been handed to the transport. This marks a *forwarding*
job completed-with-warnings, never confirms that paper physically emerged.
An exception or a missing submitted flag aborts the job. Jobs interrupted by a
restart are aborted, never silently replayed (which could duplicate printing).
Cancel-Job applies only before processing. Closing the server waits for an
active callback to finish, since interrupting a USB write can leave a partial
printed page. The callback must impose its own rendering and USB timeouts.

Integration::

    with create_server(process_document, Path('state'), port=8631) as server:
        server.serve_forever()

Only 127.0.0.1 is bindable. Install the Windows IPP queue using
http://127.0.0.1:8631/ipp/print. Document copies and A4/1200x600 rendering are
the callback's responsibility. All submitted documents stay in state/jobs.
"""
from __future__ import annotations

import json
import logging
import queue
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)
FORMATS = ("image/pwg-raster", "application/pdf")
OPERATIONS = (2, 4, 5, 6, 8, 9, 10, 11)
MAX_REQUEST = 256 * 1024 * 1024
RESOLUTION = (1200, 600, 3)
INPUT_RESOLUTIONS = (RESOLUTION, (600, 600, 3), (300, 300, 3))
DEFAULT_RESOLUTION = (600, 600, 3)
DOCUMENT_HANDLING = ("single-document", "separate-documents-collated-copies")
MEDIA = "iso_a4_210x297mm"


class IPPError(ValueError):
    def __init__(self, message: str, status: int = 0x0400):
        super().__init__(message)
        self.status = status


@dataclass
class Attribute:
    name: str
    tag: int
    values: list[Any]


def attr(name: str, tag: int, *values: Any) -> Attribute:
    return Attribute(name, tag, list(values))


@dataclass
class Message:
    version: tuple[int, int]
    code: int
    request_id: int
    groups: list[tuple[int, list[Attribute]]] = field(default_factory=list)
    data: bytes = b""

    def values(self, name: str) -> list[Any]:
        for _, attributes in self.groups:
            for item in attributes:
                if item.name == name:
                    return item.values
        return []

    def get(self, name: str, default: Any = None) -> Any:
        values = self.values(name)
        return values[0] if values else default


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos, self.tokens = data, 0, 0

    def take(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise IPPError("Truncated IPP message")
        value = self.data[self.pos:self.pos + count]
        self.pos += count
        return value

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def token(self) -> tuple[int, str, bytes]:
        self.tokens += 1
        if self.tokens > 4096 or self.pos > 1024 * 1024:
            raise IPPError("Too many IPP attributes")
        tag = self.take(1)[0]
        name = self.take(self.u16()).decode("utf-8", errors="strict")
        return tag, name, self.take(self.u16())


def _value(reader: _Reader, tag: int, raw: bytes, depth: int = 0) -> Any:
    if tag in (0x21, 0x23):
        if len(raw) != 4:
            raise IPPError("Invalid IPP integer length")
        return struct.unpack(">i", raw)[0]
    if tag == 0x22:
        if raw not in (b"\0", b"\1"):
            raise IPPError("Invalid IPP boolean")
        return raw == b"\1"
    if tag == 0x32:
        if len(raw) != 9:
            raise IPPError("Invalid IPP resolution")
        return struct.unpack(">iiB", raw)
    if tag == 0x33:
        if len(raw) != 8:
            raise IPPError("Invalid IPP range")
        return struct.unpack(">ii", raw)
    if tag == 0x34:
        if raw or depth >= 8:
            raise IPPError("Invalid or excessive IPP collection nesting")
        members: list[Attribute] = []
        member_name = None
        while True:
            member_tag, name, body = reader.token()
            if name:
                raise IPPError("Collection member encoded with attribute name")
            if member_tag == 0x37:
                if body or member_name is not None:
                    raise IPPError("Invalid IPP collection terminator")
                return members
            if member_tag == 0x4A:
                if member_name is not None:
                    raise IPPError("Collection member missing value")
                member_name = body.decode("utf-8")
                if not member_name:
                    raise IPPError("Empty collection member name")
                continue
            value = _value(reader, member_tag, body, depth + 1)
            if member_name is not None:
                members.append(attr(member_name, member_tag, value))
                member_name = None
            elif members and member_tag == members[-1].tag:
                members[-1].values.append(value)
            else:
                raise IPPError("Collection value has no member name")
    if 0x41 <= tag <= 0x49:
        return raw.decode("utf-8", errors="strict")
    return raw


def parse_message(data: bytes) -> Message:
    reader = _Reader(data)
    major, minor, code, request_id = struct.unpack(">BBHi", reader.take(8))
    message = Message((major, minor), code, request_id)
    current: list[Attribute] | None = None
    try:
        while True:
            tag = reader.data[reader.pos] if reader.pos < len(data) else -1
            if tag == 3:
                reader.pos += 1
                message.data = data[reader.pos:]
                return message
            if 1 <= tag <= 0x0F:
                reader.pos += 1
                current = []
                message.groups.append((tag, current))
                continue
            if current is None or tag < 0x10:
                raise IPPError("Missing attribute group or end-of-attributes")
            tag, name, raw = reader.token()
            value = _value(reader, tag, raw)
            if name:
                current.append(attr(name, tag, value))
            elif current and current[-1].tag == tag:
                current[-1].values.append(value)
            else:
                raise IPPError("Additional value has no matching attribute")
    except (UnicodeError, struct.error) as exc:
        raise IPPError("Malformed IPP attribute") from exc


def _record(tag: int, name: str, raw: bytes) -> bytes:
    name_bytes = name.encode("utf-8")
    return (bytes([tag]) + struct.pack(">H", len(name_bytes)) + name_bytes
            + struct.pack(">H", len(raw)) + raw)


def _encoded_value(tag: int, value: Any) -> tuple[bytes, bytes]:
    if tag in (0x21, 0x23):
        return struct.pack(">i", value), b""
    if tag == 0x22:
        return bytes([bool(value)]), b""
    if tag == 0x32:
        return struct.pack(">iiB", *value), b""
    if tag == 0x33:
        return struct.pack(">ii", *value), b""
    if tag == 0x34:
        tail = bytearray()
        for member in value:
            tail.extend(_record(0x4A, "", member.name.encode("utf-8")))
            for element in member.values:
                raw, nested = _encoded_value(member.tag, element)
                tail.extend(_record(member.tag, "", raw) + nested)
        tail.extend(_record(0x37, "", b""))
        return b"", bytes(tail)
    return value if isinstance(value, bytes) else str(value).encode("utf-8"), b""


def encode_message(message: Message) -> bytes:
    output = bytearray(struct.pack(">BBHi", *message.version, message.code, message.request_id))
    for group, attributes in message.groups:
        output.append(group)
        for item in attributes:
            for index, value in enumerate(item.values):
                raw, tail = _encoded_value(item.tag, value)
                output.extend(_record(item.tag, item.name if index == 0 else "", raw) + tail)
    return bytes(output) + b"\3" + message.data


def _operation_attributes(message: str = "") -> list[Attribute]:
    result = [attr("attributes-charset", 0x47, "utf-8"),
              attr("attributes-natural-language", 0x48, "en")]
    if message:
        result.append(attr("status-message", 0x41, message[:1023]))
    return result


def _same_collection(actual: list[Attribute], expected: list[Attribute], *, subset: bool = False) -> bool:
    """Collection member order is insignificant; duplicate members are invalid."""
    if not isinstance(actual, list) or not all(isinstance(a, Attribute) for a in actual):
        return False
    members = {a.name: a for a in actual}
    reference = {a.name: a for a in expected}
    if len(members) != len(actual) or not members.keys() <= reference.keys():
        return False
    if not subset and members.keys() != reference.keys():
        return False
    for name, item in members.items():
        other = reference[name]
        if item.tag != other.tag or len(item.values) != len(other.values):
            return False
        if item.tag == 0x34:
            if not all(_same_collection(left, right) for left, right in zip(item.values, other.values)):
                return False
        elif item.values != other.values:
            return False
    return True


def _normalize_a4_size(collection: Any) -> Any:
    """Allow Windows' 0.01 mm A4 rounding, without relaxing other members."""
    if not isinstance(collection, list):
        return collection
    normalized = []
    for member in collection:
        if (not isinstance(member, Attribute) or member.name != "media-size"
                or member.tag != 0x34 or len(member.values) != 1
                or not isinstance(member.values[0], list)):
            normalized.append(member)
            continue
        dimensions = []
        for dimension in member.values[0]:
            expected = {"x-dimension": 21000, "y-dimension": 29700}.get(dimension.name) if isinstance(dimension, Attribute) else None
            if (expected is not None and dimension.tag == 0x21
                    and len(dimension.values) == 1
                    and type(dimension.values[0]) is int
                    and abs(dimension.values[0] - expected) <= 1):
                dimensions.append(attr(dimension.name, 0x21, expected))
            else:
                dimensions.append(dimension)
        # Retain every member, including duplicates, for strict validation.
        normalized.append(attr(member.name, member.tag, dimensions))
    return normalized


class Bridge:
    """Durable metadata plus a serialized document callback queue."""

    def __init__(self, process_document: Callable, state_dir: Path, port: int):
        self.process_document = process_document
        self.directory = Path(state_dir) / "jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        # Windows directed IPP discovery needs a nonzero printer-uuid. Persist
        # it across restarts/port changes; it identifies this logical bridge,
        # not the USB printer's hardware serial number (PWG 5100.13 section 6.6).
        identity_path = Path(state_dir) / "printer-identity.json"
        if identity_path.exists():
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            identifier = uuid.UUID(identity["printer_uuid"])
            if identifier.int == 0:
                raise ValueError("Invalid zero printer UUID in printer-identity.json")
        else:
            identifier = uuid.uuid4()
            identity_path.write_text(json.dumps({"printer_uuid": str(identifier)}, indent=2), encoding="utf-8")
        self.printer_uuid = "urn:uuid:" + str(identifier)
        self.uri = f"ipp://127.0.0.1:{port}/ipp/print"
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.jobs: dict[int, dict] = {}
        self.queue: queue.Queue[int | None] = queue.Queue()
        self.closing = False
        for path in self.directory.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if path.parent.name != str(int(job["id"])):
                    continue
                self.jobs[job["id"]] = job
                if job["state"] < 7:
                    self._state(job, 8, "aborted-by-system", "Bridge restarted; job not replayed")
            except (OSError, ValueError, KeyError, TypeError):
                LOG.warning("Cannot restore job metadata %s", path)
        self.next_id = max(self.jobs, default=0) + 1
        self.worker = threading.Thread(target=self._worker, name="p1006-print", daemon=False)
        self.worker.start()

    def uptime(self) -> int:
        return max(1, int(time.monotonic() - self.started))

    def _save(self, job: dict) -> None:
        directory = self.directory / str(job["id"])
        directory.mkdir(exist_ok=True)
        temporary = directory / "job.json.tmp"
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(directory / "job.json")

    def _state(self, job: dict, state: int, reason: str, message: str) -> None:
        job.update(state=state, reason=reason, message=message)
        if state >= 7:
            job["completed_at"] = self.uptime()
        self._save(job)

    def _worker(self) -> None:
        while True:
            job_id = self.queue.get()
            try:
                if job_id is None:
                    return
                with self.lock:
                    job = self.jobs[job_id]
                    if job["state"] != 3:
                        continue
                    job["processing_at"] = self.uptime()
                    self._state(job, 5, "job-printing", "Rendering and forwarding to USB")
                    options = dict(job["options"], **{"job-id": job_id,
                                   "job-dir": str(self.directory / str(job_id))})
                data = (self.directory / str(job_id) / "document.bin").read_bytes()
                result = self.process_document(data, job["format"], options)
                if isinstance(result, dict):
                    with self.lock:
                        job["result"] = result
                        self._save(job)
                        if result.get("dry_run") is True and result.get("submitted") is not True:
                            self._state(job, 8, "aborted-by-system", str(result.get("message", "Dry run; no USB transfer")))
                            continue
                if not isinstance(result, dict) or result.get("submitted") is not True:
                    raise RuntimeError("Callback did not confirm transport submission")
                with self.lock:
                    self._state(job, 9, "job-completed-with-warnings",
                                "Submitted to USB; physical page output is not confirmed")
            except Exception as exc:
                LOG.exception("Print job %s failed", job_id)
                with self.lock:
                    if job_id in self.jobs:
                        self._state(self.jobs[job_id], 8, "aborted-by-system", str(exc)[:1023])
            finally:
                self.queue.task_done()

    def close(self) -> None:
        with self.lock:
            if self.closing:
                return
            self.closing = True
            for job in self.jobs.values():
                if job["state"] in (3, 4):
                    self._state(job, 8, "aborted-by-system", "Bridge stopped before submission")
        self.queue.put(None)
        self.worker.join()

    def _media_collection(self) -> list[Attribute]:
        return [attr("media-size", 0x34, [attr("x-dimension", 0x21, 21000),
                                          attr("y-dimension", 0x21, 29700)]),
                attr("media-size-name", 0x44, MEDIA), attr("media-type", 0x44, "stationery"),
                attr("media-source", 0x44, "auto"),
                *(attr(f"media-{edge}-margin", 0x21, 400)
                  for edge in ("bottom", "left", "right", "top"))]

    def printer_attributes(self) -> list[Attribute]:
        active = sum(j["state"] < 7 for j in self.jobs.values())
        processing = any(j["state"] == 5 for j in self.jobs.values())
        return [
            attr("printer-uri-supported", 0x45, self.uri),
            attr("printer-uuid", 0x45, self.printer_uuid),
            # This is the logical bridge's PDL support, not the raw HBS-only
            # 1284 ID returned by the USB printer. No certification is claimed.
            attr("printer-device-id", 0x41,
                 "MFG:HP;MDL:LaserJet P1006 ARM64 Bridge;CMD:PWGRaster,PDF;CLS:PRINTER;DES:Local HP P1006 IPP bridge;"),
            attr("uri-authentication-supported", 0x44, "none"),
            attr("uri-security-supported", 0x44, "none"),
            attr("printer-name", 0x42, "HP LaserJet P1006 ARM64 Bridge"),
            attr("printer-info", 0x41, "Local P1006 host raster bridge"),
            attr("printer-location", 0x41, "USB on this computer"),
            attr("printer-make-and-model", 0x41, "HP LaserJet P1006"),
            attr("printer-state", 0x23, 4 if processing else 3),
            attr("printer-state-reasons", 0x44, "none"),
            attr("printer-state-message", 0x41, "USB hardware status is not monitored"),
            attr("printer-is-accepting-jobs", 0x22, not self.closing),
            attr("queued-job-count", 0x21, active),
            attr("printer-up-time", 0x21, self.uptime()),
            attr("printer-config-change-time", 0x21, 1),
            attr("printer-state-change-time", 0x21, self.uptime()),
            attr("ipp-versions-supported", 0x44, "1.1", "2.0"),
            attr("operations-supported", 0x23, *OPERATIONS),
            attr("charset-configured", 0x47, "utf-8"),
            attr("charset-supported", 0x47, "utf-8"),
            attr("natural-language-configured", 0x48, "en"),
            attr("generated-natural-language-supported", 0x48, "en"),
            attr("document-format-default", 0x49, "application/octet-stream"),
            attr("document-format-supported", 0x49, *FORMATS, "application/octet-stream"),
            attr("compression-supported", 0x44, "none"),
            attr("color-supported", 0x22, False),
            attr("print-color-mode-default", 0x44, "monochrome"),
            attr("print-color-mode-supported", 0x44, "monochrome"),
            attr("sides-default", 0x44, "one-sided"),
            attr("sides-supported", 0x44, "one-sided"),
            attr("copies-default", 0x21, 1), attr("copies-supported", 0x33, (1, 99)),
            attr("printer-resolution-default", 0x32, DEFAULT_RESOLUTION),
            attr("printer-resolution-supported", 0x32, *INPUT_RESOLUTIONS),
            attr("pwg-raster-document-resolution-supported", 0x32, *INPUT_RESOLUTIONS),
            attr("pwg-raster-document-type-supported", 0x44, "black_1", "sgray_1", "sgray_8"),
            attr("pwg-raster-document-sheet-back", 0x44, "normal"),
            attr("media-default", 0x44, MEDIA), attr("media-ready", 0x44, MEDIA),
            attr("media-supported", 0x44, MEDIA),
            attr("media-col-default", 0x34, self._media_collection()),
            attr("media-col-ready", 0x34, self._media_collection()),
            attr("media-col-database", 0x34, self._media_collection()),
            attr("media-col-supported", 0x44, "media-size", "media-size-name", "media-type",
                 "media-source", "media-bottom-margin", "media-left-margin", "media-right-margin", "media-top-margin"),
            attr("media-size-supported", 0x34, [attr("x-dimension", 0x21, 21000), attr("y-dimension", 0x21, 29700)]),
            attr("media-source-supported", 0x44, "auto"), attr("media-type-supported", 0x44, "stationery"),
            *(attr(f"media-{edge}-margin-supported", 0x21, 400) for edge in ("bottom", "left", "right", "top")),
            attr("multiple-document-jobs-supported", 0x22, False),
            attr("multiple-document-handling-default", 0x44, "single-document"),
            attr("multiple-document-handling-supported", 0x44, *DOCUMENT_HANDLING),
            attr("job-creation-attributes-supported", 0x44, "copies", "media", "media-col", "printer-resolution", "sides", "print-color-mode"),
            attr("job-k-octets-supported", 0x33, (0, MAX_REQUEST // 1024)),
        ]

    def _job_attributes(self, job: dict) -> list[Attribute]:
        result = [attr("job-id", 0x21, job["id"]),
                  attr("job-uri", 0x45, self.uri + "/jobs/" + str(job["id"])),
                  attr("job-printer-uri", 0x45, self.uri),
                  attr("job-name", 0x42, job["name"]),
                  attr("job-originating-user-name", 0x42, job["user"]),
                  attr("job-state", 0x23, job["state"]),
                  attr("job-state-reasons", 0x44, job["reason"]),
                  attr("job-state-message", 0x41, job["message"]),
                  attr("time-at-creation", 0x21, job["created_at"]),
                  attr("job-printer-up-time", 0x21, self.uptime())]
        for key, name in (("processing_at", "time-at-processing"), ("completed_at", "time-at-completed")):
            result.append(attr(name, 0x21, job[key]) if key in job else attr(name, 0x13, b""))
        return result

    def _options(self, request: Message) -> dict:
        options = {"copies": request.get("copies", 1), "media": request.get("media", MEDIA),
                   "printer-resolution": request.get("printer-resolution", DEFAULT_RESOLUTION),
                   "sides": request.get("sides", "one-sided"),
                   "print-color-mode": request.get("print-color-mode", "monochrome"),
                   "multiple-document-handling": request.get("multiple-document-handling", "single-document")}
        if type(options["copies"]) is not int or not 1 <= options["copies"] <= 99:
            raise IPPError("copies must be between 1 and 99", 0x040B)
        if options["printer-resolution"] not in INPUT_RESOLUTIONS:
            raise IPPError("Unsupported printer-resolution", 0x040B)
        for name, expected in (("media", MEDIA),
                               ("sides", "one-sided"), ("print-color-mode", "monochrome")):
            if options[name] != expected:
                raise IPPError(f"Unsupported {name}", 0x040B)
        if request.get("compression", "none") != "none":
            raise IPPError("Compression is unsupported", 0x040F)
        if options["multiple-document-handling"] not in DOCUMENT_HANDLING:
            raise IPPError("Unsupported multiple-document-handling; copies must be collated", 0x040B)
        # Never silently ignore settings that change actual output.
        for name in ("page-ranges", "number-up", "finishings", "orientation-requested"):
            if request.values(name) and not (name == "number-up" and request.get(name) == 1) and not (name == "finishings" and request.get(name) == 3) and not (name == "orientation-requested" and request.get(name) == 3):
                raise IPPError(f"Unsupported {name}; render this setting in the client", 0x040B)
        collection = request.get("media-col")
        if collection is not None:
            if not _same_collection(_normalize_a4_size(collection), self._media_collection(), subset=True):
                raise IPPError("Unsupported or malformed media-col", 0x040B)
        return options

    def _format(self, request: Message, *, require_document: bool = False) -> str:
        mime = request.get("document-format", "application/octet-stream")
        if mime == "application/octet-stream" and request.data:
            mime = "image/pwg-raster" if request.data.startswith(b"RaS2") else "application/pdf" if request.data.startswith(b"%PDF-") else mime
        if mime not in FORMATS and not (not require_document and mime == "application/octet-stream"):
            raise IPPError("Unsupported document-format", 0x040A)
        if require_document and not request.data:
            raise IPPError("Empty document", 0x0411)
        return mime

    def _new_job(self, request: Message, options: dict) -> dict:
        if sum(job["state"] < 7 for job in self.jobs.values()) >= 32:
            raise IPPError("Local print queue is full", 0x0507)
        job_id = self.next_id
        self.next_id += 1
        job = dict(id=job_id, name=str(request.get("job-name", "Untitled"))[:255],
                   user=str(request.get("requesting-user-name", "local"))[:255], options=options,
                   state=4, reason="job-incoming", message="Waiting for document",
                   created_at=self.uptime())
        self.jobs[job_id] = job
        self._save(job)
        return job

    def _submit(self, job: dict, request: Message) -> None:
        mime = self._format(request, require_document=True)
        (self.directory / str(job["id"]) / "document.bin").write_bytes(request.data)
        job["format"] = mime
        self._state(job, 3, "none", "Waiting for the USB worker")
        self.queue.put(job["id"])

    def _find_job(self, request: Message) -> dict:
        job_id = request.get("job-id")
        if job_id is None:
            uri = request.get("job-uri", "")
            path = self._local_uri_path(uri)
            prefix = "/ipp/print/jobs/"
            if path and path.startswith(prefix) and path[len(prefix):].isdigit():
                job_id = int(path[len(prefix):])
        if type(job_id) is not int or job_id not in self.jobs:
            raise IPPError("Job not found", 0x0406)
        return self.jobs[job_id]

    def _local_uri_path(self, uri: Any) -> str | None:
        """Accept Windows' HTTP spelling only for this exact loopback endpoint."""
        if not isinstance(uri, str):
            return None
        try:
            target = urlsplit(uri)
            local = urlsplit(self.uri)
            if (target.scheme not in ("ipp", "http") or target.hostname != local.hostname
                    or target.port != local.port or target.username is not None
                    or target.password is not None or target.query or target.fragment):
                return None
            return target.path
        except ValueError:
            return None

    def handle(self, request: Message) -> Message:
        version = request.version if request.version in ((1, 1), (2, 0)) else (2, 0)
        response = Message(version, 0, request.request_id, [(1, _operation_attributes())])
        try:
            if request.version not in ((1, 1), (2, 0)):
                raise IPPError("Unsupported IPP version", 0x0503)
            if request.get("attributes-charset") != "utf-8":
                raise IPPError("Only utf-8 is supported", 0x040D)
            if self._local_uri_path(request.get("printer-uri", self.uri)) != "/ipp/print":
                raise IPPError("Printer not found", 0x0406)
            if request.code not in OPERATIONS:
                raise IPPError("Operation not supported", 0x0501)
            requested = request.values("requested-attributes")
            def filtered(values: list[Attribute]) -> list[Attribute]:
                return values if not requested or "all" in requested or any(v in requested for v in ("printer-description", "job-description", "job-template")) else [v for v in values if v.name in requested]
            with self.lock:
                if request.code == 11:
                    response.groups.append((4, filtered(self.printer_attributes())))
                elif request.code in (2, 4, 5):
                    if self.closing:
                        raise IPPError("Bridge is stopping", 0x0506)
                    options = self._options(request)
                    self._format(request, require_document=request.code == 2)
                    if request.code != 4:
                        job = self._new_job(request, options)
                        if request.code == 2:
                            self._submit(job, request)
                        response.groups.append((2, self._job_attributes(job)))
                elif request.code == 6:
                    job = self._find_job(request)
                    if job["state"] != 4:
                        raise IPPError("Job is no longer waiting for a document", 0x0404)
                    if request.get("last-document") is not True:
                        raise IPPError("Only one document per job is supported", 0x0509)
                    self._submit(job, request)
                    response.groups.append((2, self._job_attributes(job)))
                elif request.code in (8, 9):
                    job = self._find_job(request)
                    if request.code == 8:
                        if job["state"] not in (3, 4):
                            raise IPPError("Cannot cancel a job already submitted or being written to USB", 0x0404)
                        self._state(job, 7, "job-canceled-by-user", "Canceled before transport submission")
                    else:
                        response.groups.append((2, filtered(self._job_attributes(job))))
                elif request.code == 10:
                    which = request.get("which-jobs", "not-completed")
                    if which not in ("all", "completed", "not-completed"):
                        raise IPPError("Unsupported which-jobs", 0x040B)
                    limit = request.get("limit", 1000)
                    if type(limit) is not int or limit < 1:
                        raise IPPError("Invalid job limit")
                    selected = [job for job in self.jobs.values()
                                if (which == "all" or (job["state"] >= 7) == (which == "completed"))
                                and (not request.get("my-jobs", False) or job["user"] == request.get("requesting-user-name"))]
                    for job in selected[:min(limit, 1000)]:
                        response.groups.append((2, filtered(self._job_attributes(job))))
        except IPPError as exc:
            response.code = exc.status
            response.groups = [(1, _operation_attributes(str(exc)))]
        return response


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "P1006Bridge/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        LOG.info("HTTP %s", format % args)

    def _read_exact(self, count: int) -> bytes:
        data = self.rfile.read(count)
        if len(data) != count:
            raise IPPError("Truncated HTTP body")
        return data

    def _body(self) -> bytes:
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        lengths = self.headers.get_all("Content-Length", [])
        if transfer:
            if transfer != "chunked" or lengths:
                raise IPPError("Invalid HTTP body framing")
            output = bytearray()
            while True:
                line = self.rfile.readline(128)
                if not line.endswith(b"\r\n"):
                    raise IPPError("Invalid chunk header")
                try:
                    size = int(line.split(b";", 1)[0], 16)
                except ValueError as exc:
                    raise IPPError("Invalid chunk length") from exc
                if size < 0 or len(output) + size > self.server.max_request:
                    raise IPPError("Request exceeds byte limit", 0x0408)
                if size == 0:
                    trailer_bytes = 0
                    while True:
                        trailer = self.rfile.readline(8192)
                        trailer_bytes += len(trailer)
                        if not trailer.endswith(b"\r\n") or trailer_bytes > 65536:
                            raise IPPError("Invalid HTTP trailer")
                        if trailer == b"\r\n":
                            return bytes(output)
                output.extend(self._read_exact(size))
                if self._read_exact(2) != b"\r\n":
                    raise IPPError("Invalid chunk terminator")
        if len(lengths) != 1:
            raise IPPError("A single Content-Length or chunked body is required")
        try:
            length = int(lengths[0])
        except ValueError as exc:
            raise IPPError("Invalid Content-Length") from exc
        if length < 0 or length > self.server.max_request:
            raise IPPError("Request exceeds byte limit", 0x0408)
        return self._read_exact(length)

    def do_POST(self):
        if urlsplit(self.path).path != "/ipp/print":
            self.close_connection = True
            self.send_error(404)
            return
        if self.headers.get_content_type() != "application/ipp":
            self.close_connection = True
            self.send_error(415)
            return
        try:
            request = parse_message(self._body())
        except (IPPError, OSError, TimeoutError, ValueError) as exc:
            self.close_connection = True
            self.send_error(413 if isinstance(exc, IPPError) and exc.status == 0x0408 else 400, str(exc))
            return
        try:
            response = self.server.bridge.handle(request)
        except Exception:
            LOG.exception("IPP processing failed")
            response = Message((2, 0), 0x0500, request.request_id,
                               [(1, _operation_attributes("Internal bridge error; see log"))])
        body = encode_message(response)
        LOG.info("IPP operation=0x%04x request_id=%d version=%d.%d document_bytes=%d status=0x%04x",
                 request.code, request.request_id, *request.version, len(request.data), response.code)
        if request.code in (2, 4, 5, 6):
            # Log settings needed to diagnose real Windows driver requests;
            # document bytes, document/job titles and user names stay out.
            settings = {item.name: item.values for _, items in request.groups
                        for item in items if item.name not in
                        ("requesting-user-name", "job-name", "document-name")}
            LOG.info("IPP job request_id=%d status_message=%s settings=%s",
                     request.request_id, json.dumps(response.get("status-message")),
                     json.dumps(settings, ensure_ascii=True, default=lambda value:
                                value.__dict__ if isinstance(value, Attribute) else str(value))[:16384])
        if request.code == 11:
            # Discovery diagnostics only: no document, job name or user name.
            # JSON escapes newlines in peer-provided values before logging.
            discovery = {
                "requested": request.values("requested-attributes"),
                "document_format": request.get("document-format"),
                "returned": [item.name for group, items in response.groups
                             if group == 4 for item in items],
            }
            LOG.info("IPP discovery request_id=%d attributes=%s", request.request_id,
                     json.dumps(discovery, ensure_ascii=True, default=str)[:16384])
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        body = b"HP LaserJet P1006 local IPP bridge. POST application/ipp to /ipp/print.\nPhysical USB printer status is not monitored.\n"
        self.send_response(200 if self.path in ("/", "/ipp/print") else 404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR permits a second listener on the same port. That
    # would also let its Bridge recovery abort the first instance's live jobs.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def server_close(self):
        super().server_close()
        if hasattr(self, "bridge"):
            self.bridge.close()


def create_server(process_document: Callable[[bytes, str, dict], dict],
                  state_dir: Path, port: int = 8631, *, max_request: int = MAX_REQUEST) -> BridgeServer:
    """Create a loopback-only server; caller owns serve_forever/shutdown/close."""
    # Acquire the listening port before Bridge can recover persisted jobs.
    server = BridgeServer(("127.0.0.1", port), _Handler)
    server.max_request = max_request
    server.bridge = Bridge(process_document, Path(state_dir), server.server_address[1])
    return server
