"""Prepare the volatile firmware from the user's original HP package.

Framing follows OpenPrinting/foo2zjs arm2hpdl.c, dated IMG (ispjl) branch.
This uploads runtime firmware to RAM; it does not flash persistent storage.
"""

import hashlib
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HP_PACKAGE = ROOT / "ljP1000_P1500-HB-pnp-win64-ru.exe"
PACKAGE_SHA256 = "6dc51bbbd0ff3e79a97603c34bc865f73fbaf529e3f54ffceaafe477f1ea8a61"
FIRMWARE_IMG = ROOT / ".vendor/hp/P1006.img"
FIRMWARE_SHA256 = "4031deea33ca48d66d93429f3f847b43c46446b8438e2717da9a9a1ea8e43669"
UEL = b"\x1b%-12345X"


def firmware_bytes(path: Path = FIRMWARE_IMG) -> bytes:
    image = path.read_bytes()
    if hashlib.sha256(image).hexdigest() != FIRMWARE_SHA256:
        raise ValueError("Unexpected firmware. Expected P1006.img from the supplied HP package.")
    if image[:8] != b"20100824" or image[8:12] != b"\x7fELF":
        raise ValueError("Invalid P1006 firmware header")
    # The length excludes the date, but the transmitted payload INCLUDES it.
    return (UEL + b"@PJL ENTER LANGUAGE=ACL\r\n" + b"\x00\xac\xc0\xde"
            + struct.pack(">I", len(image) - 8) + image + UEL)


def has_firmware(device_id: str) -> bool:
    return any(part.strip().upper().startswith("FWVER:") and part.split(":", 1)[1].strip()
               for part in device_id.split(";"))
