#!/usr/bin/env python3
"""Recover a bricked Bowers & Wilkins Zeppelin Air over USB.

The Zeppelin Air contains an Atmel AVR32 UC3A0256 host MCU and a separate
"DMP" coprocessor that handles AirPlay, networking and audio DSP. When a
firmware update fails the MCU is left sitting in its Atmel DFU bootloader
(white LED) and the speaker will not play anything.

B&W's official recovery utility performs a two step repair. Step 2 fails on
many units, and the utility ships as a 32 bit i386 binary that no longer runs
on modern macOS. This tool reimplements the whole procedure, including the
undocumented protocol extensions B&W added to the stock Atmel bootloader.

See docs/PROTOCOL.md for how the protocol was recovered and what each command
does on the wire.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:  # pragma: no cover - dependency check
    sys.exit("pyusb is required: pip install -r requirements.txt")


# --------------------------------------------------------------------------
# Device identity and protocol constants
# --------------------------------------------------------------------------

VID = 0x03EB          # Atmel
PID = 0x2FF8          # AVR32 UC3 DFU bootloader, as shipped in the Zeppelin Air
INTERFACE = 0

# DFU 1.1 class requests
DFU_DNLOAD = 1
DFU_UPLOAD = 2
DFU_GETSTATUS = 3
DFU_CLRSTATUS = 4

# bmRequestType: class request to an interface
OUT = 0x21
IN = 0xA1

TIMEOUT = 20_000      # ms, generous: erase and coprocessor ops are slow

# Atmel AVR32 packet geometry. The control block is 64 bytes on AVR32 parts
# (32 on classic AVR), and every write is followed by a 16 byte DFU suffix.
CONTROL_BLOCK = 0x40
MAX_TRANSFER = 0x400
FOOTER = 0x10
PAGE_SIZE = 0x10000   # the bootloader addresses flash in 64 KiB pages

# 16 byte DFU file suffix appended to every write. bcdDFU 0x0110, and the
# vendor/product/device and CRC fields left as 0xFF (the bootloader ignores
# them, but it does require the suffix to be present and well formed).
DFU_SUFFIX = bytes([0x00, 0x00, 0x00, 0x00, 0x10, 0x44, 0x46, 0x55,
                    0x01, 0x10, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])

# Memory units selectable with "06 03 00 <unit>".
UNIT_FLASH = 0
UNIT_EEPROM = 1
UNIT_SECURITY = 2
UNIT_CONFIG = 3
UNIT_BOOTLOADER = 4
UNIT_SIGNATURE = 5
UNIT_USER = 6
UNIT_COPROC = 7          # B&W extension: the DMP coprocessor
UNIT_COPROC_BOOT = 8     # B&W extension: coprocessor bootloader
UNIT_COPROC_XTRA = 9     # B&W extension

# Coprocessor readiness, reported in the iString byte of DFU_GETSTATUS.
COPROC_WAITING = (0x00, 0x01, 0x02, 0x03, 0x11)
COPROC_READY = 0x12

# Default coprocessor init timeouts, in seconds, taken from the official
# updater (it hardcodes 5 and 15).
COPROC_STARTUP_TIMEOUT = 5
COPROC_READY_TIMEOUT = 15

# Flash below 0x3900 cannot be verified by readback, for two separate reasons:
#
#   0x0000-0x1FFF  The bootloader itself, write protected by the BOOTPROT fuse.
#                  B&W's hex files do contain code for this range, but writes
#                  are ignored and reads come back as 0xFF.
#   0x2000-0x38FF  A second stage bootloader that survives a chip erase. The
#                  hex files ship zeros here while the device holds real data,
#                  byte identical before and after an erase.
#
# Writes are still issued over this range, exactly as the official utility
# does, but a verify pass must skip it or it reports thousands of benign
# mismatches. This is the "5841 invalid bytes" error dfu-programmer reports.
PROTECTED_REGION = (0x0000, 0x3900)

DFU_STATUS = {
    0x00: "OK",
    0x01: "errTARGET",
    0x02: "errFILE",
    0x03: "errWRITE",
    0x04: "errERASE",
    0x05: "errCHECK_ERASED",
    0x06: "errPROG",
    0x07: "errVERIFY",
    0x08: "errADDRESS",
    0x09: "errNOTDONE",
    0x0A: "errFIRMWARE",
    0x0B: "errVENDOR",
    0x0C: "errUSBR",
    0x0D: "errPOR",
    0x0E: "errUNKNOWN",
    0x0F: "errSTALLEDPKT",
}


class RecoveryError(Exception):
    """Raised for any unrecoverable protocol or device failure."""


# --------------------------------------------------------------------------
# Intel HEX
# --------------------------------------------------------------------------

def parse_ihex(path: str) -> dict[int, int]:
    """Parse an Intel HEX file into a sparse {address: byte} map.

    Handles record types 0x00 (data), 0x01 (EOF), 0x02/0x04 (segment and
    linear address extension). AVR32 hex files address flash from
    0x80000000, which is masked off so addresses are flash relative.
    """
    memory: dict[int, int] = {}
    base = 0

    with open(path, "r") as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            if not line.startswith(":"):
                raise RecoveryError(f"{path}:{lineno}: record does not start with ':'")

            try:
                raw = bytes.fromhex(line[1:])
            except ValueError as exc:
                raise RecoveryError(f"{path}:{lineno}: invalid hex: {exc}") from exc

            if len(raw) < 5:
                raise RecoveryError(f"{path}:{lineno}: record too short")
            if (sum(raw) & 0xFF) != 0:
                raise RecoveryError(f"{path}:{lineno}: checksum mismatch")

            count, offset, rectype = raw[0], (raw[1] << 8) | raw[2], raw[3]
            data = raw[4:4 + count]
            if len(data) != count:
                raise RecoveryError(f"{path}:{lineno}: truncated record")

            if rectype == 0x00:
                for i, byte in enumerate(data):
                    memory[(base + offset + i) & 0x7FFFFFFF] = byte
            elif rectype == 0x01:
                break
            elif rectype == 0x02:
                base = ((data[0] << 8) | data[1]) << 4
            elif rectype == 0x04:
                base = ((data[0] << 8) | data[1]) << 16
            # 0x03 and 0x05 carry start addresses and are irrelevant here.

    if not memory:
        raise RecoveryError(f"{path}: no data records found")
    return memory


def contiguous_runs(memory: dict[int, int]) -> list[tuple[int, bytes]]:
    """Collapse a sparse memory map into (start, data) runs."""
    runs: list[tuple[int, bytes]] = []
    start = None
    buf = bytearray()

    for addr in sorted(memory):
        if start is not None and addr == start + len(buf):
            buf.append(memory[addr])
            continue
        if start is not None:
            runs.append((start, bytes(buf)))
        start, buf = addr, bytearray([memory[addr]])

    if start is not None:
        runs.append((start, bytes(buf)))
    return runs


# --------------------------------------------------------------------------
# DFU transport
# --------------------------------------------------------------------------

class DfuStatus:
    __slots__ = ("status", "poll_timeout", "state", "coproc_state")

    def __init__(self, raw: bytes):
        if len(raw) != 6:
            raise RecoveryError(f"short DFU_GETSTATUS reply: {len(raw)} bytes")
        self.status = raw[0]
        self.poll_timeout = raw[1] | (raw[2] << 8) | (raw[3] << 16)
        self.state = raw[4]
        # Stock DFU calls this iString. B&W reuse it to report coprocessor
        # readiness, which is the key to making the coprocessor respond.
        self.coproc_state = raw[5]

    @property
    def ok(self) -> bool:
        return self.status == 0x00

    def __str__(self) -> str:
        name = DFU_STATUS.get(self.status, "?")
        return f"bStatus=0x{self.status:02x} ({name}) bState=0x{self.state:02x}"


class Dfu:
    """Minimal DFU 1.1 transport over USB control transfers."""

    def __init__(self, dev, interface: int = INTERFACE):
        self.dev = dev
        self.interface = interface
        self.transaction = 0

    def download(self, data: bytes) -> int:
        sent = self.dev.ctrl_transfer(OUT, DFU_DNLOAD, self.transaction,
                                      self.interface, data, TIMEOUT)
        self.transaction = (self.transaction + 1) & 0xFFFF
        if sent != len(data):
            raise RecoveryError(f"short DFU_DNLOAD: sent {sent} of {len(data)}")
        return sent

    def upload(self, length: int) -> bytes:
        data = self.dev.ctrl_transfer(IN, DFU_UPLOAD, self.transaction,
                                      self.interface, length, TIMEOUT)
        self.transaction = (self.transaction + 1) & 0xFFFF
        return bytes(data)

    def get_status(self) -> DfuStatus:
        return DfuStatus(bytes(self.dev.ctrl_transfer(
            IN, DFU_GETSTATUS, 0, self.interface, 6, TIMEOUT)))

    def clear_status(self) -> None:
        try:
            self.dev.ctrl_transfer(OUT, DFU_CLRSTATUS, 0, self.interface, None, TIMEOUT)
        except usb.core.USBError:
            pass


# --------------------------------------------------------------------------
# Atmel AVR32 bootloader plus B&W extensions
# --------------------------------------------------------------------------

class Zeppelin:
    def __init__(self, dfu: Dfu, verbose: bool = False):
        self.dfu = dfu
        self.verbose = verbose
        self._unit = None
        self._page = None

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"    {message}")

    # -- command helpers ---------------------------------------------------

    def _command(self, payload: bytes, what: str, allow_fail: bool = False) -> DfuStatus:
        self.dfu.download(payload)
        status = self.dfu.get_status()
        self.log(f"{what}: {payload.hex(' ')} -> {status}")
        if not status.ok and not allow_fail:
            self.dfu.clear_status()
            raise RecoveryError(f"{what} failed: {status}")
        return status

    def select_ena(self, value: int = 1) -> None:
        """Send B&W's undocumented command interface enable.

        This is the single most important discovery: the Zeppelin Air's
        bootloader silently rejects every memory command unless "06 06 01 00"
        is sent first. Stock dfu-programmer never sends it, which is why all
        off the shelf tooling fails with a pipe error on this device.
        """
        self._command(bytes([0x06, 0x06, value & 0xFF, 0x00]), "select_ena")

    def select_memory_unit(self, unit: int) -> None:
        self.select_ena()
        self._command(bytes([0x06, 0x03, 0x00, unit]), f"select_memory_unit({unit})")
        self._unit = unit
        self._page = None

    def select_page(self, page: int) -> None:
        if self._page == page:
            return
        self.select_ena()
        self._command(bytes([0x06, 0x03, 0x01, 0x00,
                             (page >> 8) & 0xFF, page & 0xFF]), f"select_page({page})")
        self._page = page

    # -- inspection --------------------------------------------------------

    def bootloader_version(self) -> int:
        self.select_memory_unit(UNIT_BOOTLOADER)
        self.dfu.download(bytes([0x03, 0x00, 0x00, 0x00, 0x00, 0x00]))
        return self.dfu.upload(1)[0]

    # -- erase -------------------------------------------------------------

    def erase(self, retries: int = 120) -> None:
        """Full chip erase.

        The bootloader answers errNOTDONE, or stalls outright, while the erase
        is in flight. Both mean "still busy", so poll until it settles.
        """
        self.select_memory_unit(UNIT_FLASH)
        self.select_ena()
        self.dfu.download(bytes([0x04, 0x00, 0xFF]))

        for _ in range(retries):
            try:
                status = self.dfu.get_status()
            except usb.core.USBError:
                self.dfu.clear_status()
                time.sleep(0.5)
                continue
            if status.ok:
                self._page = None
                return
            if status.status == 0x09:  # errNOTDONE
                time.sleep(0.5)
                continue
            self.dfu.clear_status()
            raise RecoveryError(f"erase failed: {status}")
        raise RecoveryError("erase did not complete")

    # -- MCU flash ---------------------------------------------------------

    def _write_block(self, offset: int, data: bytes) -> None:
        """Write one block within the currently selected 64 KiB page.

        The data is placed at CONTROL_BLOCK + (offset % CONTROL_BLOCK) so that
        it lands on a 64 byte boundary relative to the flash address, matching
        the alignment the bootloader expects.
        """
        alignment = offset % CONTROL_BLOCK
        end = offset + len(data) - 1

        packet = bytearray(CONTROL_BLOCK + alignment + len(data) + FOOTER)
        packet[0] = 0x01
        packet[1] = 0x00
        packet[2] = (offset >> 8) & 0xFF
        packet[3] = offset & 0xFF
        packet[4] = (end >> 8) & 0xFF
        packet[5] = end & 0xFF
        packet[CONTROL_BLOCK + alignment:CONTROL_BLOCK + alignment + len(data)] = data
        packet[CONTROL_BLOCK + alignment + len(data):] = DFU_SUFFIX

        self.dfu.download(bytes(packet))
        status = self.dfu.get_status()
        if not status.ok:
            self.dfu.clear_status()
            raise RecoveryError(f"write at 0x{offset:04x} failed: {status}")

    def flash_mcu(self, path: str) -> int:
        """Flash an Intel HEX image into the MCU's program flash."""
        memory = parse_ihex(path)
        runs = contiguous_runs(memory)
        total = sum(len(data) for _, data in runs)
        print(f"  {os.path.basename(path)}: {total} bytes in {len(runs)} run(s)")

        self.select_memory_unit(UNIT_FLASH)
        written = 0

        for start, data in runs:
            addr = start
            view = memoryview(data)
            while view:
                page = addr // PAGE_SIZE
                offset = addr % PAGE_SIZE
                # Never cross a page boundary, never exceed the max transfer,
                # and keep blocks aligned so alignment padding stays at zero.
                chunk = min(len(view), MAX_TRANSFER, PAGE_SIZE - offset)
                if offset % MAX_TRANSFER:
                    chunk = min(chunk, MAX_TRANSFER - (offset % MAX_TRANSFER))

                self.select_page(page)
                self._write_block(offset, bytes(view[:chunk]))

                addr += chunk
                written += chunk
                view = view[chunk:]
                progress(written, total, "  flashing MCU")

        print()
        return written

    def read_flash(self, start: int, length: int) -> bytes:
        """Read program flash, honouring page boundaries."""
        self.select_memory_unit(UNIT_FLASH)
        out = bytearray()
        addr = start

        while len(out) < length:
            page = addr // PAGE_SIZE
            offset = addr % PAGE_SIZE
            chunk = min(length - len(out), PAGE_SIZE - offset, MAX_TRANSFER)

            self.select_page(page)
            self.select_ena()
            end = offset + chunk - 1
            self.dfu.download(bytes([0x03, 0x00,
                                     (offset >> 8) & 0xFF, offset & 0xFF,
                                     (end >> 8) & 0xFF, end & 0xFF]))
            out += self.dfu.upload(chunk)
            addr += chunk
            progress(len(out), length, "  reading")

        print()
        return bytes(out)

    def verify_mcu(self, path: str) -> tuple[int, int]:
        """Compare flash against a hex image, ignoring the protected region.

        Returns (mismatches, compared).
        """
        memory = parse_ihex(path)
        runs = contiguous_runs(memory)
        mismatches = compared = 0

        for start, expected in runs:
            actual = self.read_flash(start, len(expected))
            for i, (want, got) in enumerate(zip(expected, actual)):
                addr = start + i
                if PROTECTED_REGION[0] <= addr < PROTECTED_REGION[1]:
                    continue
                compared += 1
                if want != got:
                    mismatches += 1
        return mismatches, compared

    # -- coprocessor -------------------------------------------------------

    def coproc_init(self, startup: int = COPROC_STARTUP_TIMEOUT,
                    ready: int = COPROC_READY_TIMEOUT) -> None:
        """Power up the DMP coprocessor.

        Only works once per power cycle. Issuing it a second time without
        cutting mains power leaves the state machine stuck at 0x02 forever.
        """
        self.select_ena()
        self._command(bytes([0x06, 0x09,
                             (startup >> 8) & 0xFF, startup & 0xFF,
                             (ready >> 8) & 0xFF, ready & 0xFF]), "coproc_init")

    def wait_for_coproc(self, timeout: int = 300) -> None:
        """Poll until the coprocessor reports ready (state 0x12).

        This is the step every other attempt at this repair misses. Selecting
        the coprocessor memory unit before it reports ready returns errPROG,
        which looks exactly like a dead coprocessor but simply means "not
        booted yet". A healthy unit takes 20 to 30 seconds.
        """
        status = self.dfu.get_status()
        if status.coproc_state == COPROC_READY:
            print("  coprocessor already ready")
            return

        last = None
        stalled = 0
        for elapsed in range(timeout):
            status = self.dfu.get_status()
            state = status.coproc_state

            if state != last:
                print(f"  t={elapsed:3d}s  state=0x{state:02x}")
                last, stalled = state, 0
            else:
                stalled += 1
                if stalled >= 60:
                    raise RecoveryError(
                        f"coprocessor stuck at 0x{state:02x} for 60s. It needs a "
                        "full mains power cycle before init will work again.")

            if state == COPROC_READY:
                print("  coprocessor READY")
                return
            if state not in COPROC_WAITING:
                raise RecoveryError(f"coprocessor reported error state 0x{state:02x}")
            time.sleep(1)

        raise RecoveryError("coprocessor never became ready")

    def flash_coproc(self, path: str, unit: int = UNIT_COPROC) -> int:
        """Stream a coprocessor image (app_DMP.bcd).

        Every packet carries a start address of zero; the coprocessor tracks
        its own write position. Blocks are always a full 1024 bytes, zero
        padded at the tail of the file.
        """
        size = os.path.getsize(path)
        total = (size + MAX_TRANSFER - 1) // MAX_TRANSFER
        print(f"  {os.path.basename(path)}: {size} bytes in {total} blocks")

        self.select_ena()
        self._command(bytes([0x06, 0x03, 0x00, unit]), f"select_coproc({unit})")

        with open(path, "rb") as handle:
            for index in range(total):
                data = handle.read(MAX_TRANSFER)
                if len(data) < MAX_TRANSFER:
                    data += bytes(MAX_TRANSFER - len(data))

                packet = bytearray(CONTROL_BLOCK + MAX_TRANSFER + FOOTER)
                end = MAX_TRANSFER - 1
                packet[0] = 0x01
                packet[1] = 0x00
                packet[2] = 0x00           # start address is always zero
                packet[3] = 0x00
                packet[4] = (end >> 8) & 0xFF
                packet[5] = end & 0xFF
                packet[CONTROL_BLOCK:CONTROL_BLOCK + MAX_TRANSFER] = data
                packet[CONTROL_BLOCK + MAX_TRANSFER:] = DFU_SUFFIX

                self.dfu.download(bytes(packet))
                status = self.dfu.get_status()
                if not status.ok:
                    self.dfu.clear_status()
                    raise RecoveryError(f"coprocessor block {index} failed: {status}")

                progress(index + 1, total, "  flashing coprocessor")

        print()
        time.sleep(2)   # the official updater pauses here before the handshake
        self._command(bytes([0x06, 0x04, unit, 0x00]), "coproc_end_of_transfer")
        return size

    # -- launch ------------------------------------------------------------

    def start_application(self) -> None:
        """Leave the bootloader and run the application firmware.

        Uses B&W's start sequence, not the watchdog reset. A watchdog reset
        re-runs the bootloader, which re-enters ISP and lands back on a white
        LED; this jumps straight to the application instead.
        """
        self.select_ena()
        self.dfu.download(bytes([0x04, 0x03, 0x01, 0x00, 0x00]))
        try:
            self.dfu.download(b"")   # zero length download manifests the jump
        except usb.core.USBError:
            pass                     # the device is already gone, which is success


# --------------------------------------------------------------------------
# Device discovery
# --------------------------------------------------------------------------

def open_device(required: bool = True):
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        if not required:
            return None
        raise RecoveryError(
            "No Zeppelin Air DFU device found.\n"
            "Put it into its bootloader first:\n"
            "  1. Unplug the mains cable and wait for the LED to go black.\n"
            "  2. Press and hold the Standby button.\n"
            "  3. While still holding it, plug the mains cable back in.\n"
            "  4. The LED should light white.\n"
            "Also make sure the USB cable is connected to this computer.")

    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass   # already configured

    if sys.platform.startswith("linux"):
        try:
            if dev.is_kernel_driver_active(INTERFACE):
                dev.detach_kernel_driver(INTERFACE)
        except (usb.core.USBError, NotImplementedError):
            pass

    try:
        usb.util.claim_interface(dev, INTERFACE)
    except usb.core.USBError as exc:
        raise RecoveryError(f"could not claim the DFU interface: {exc}") from exc

    dfu = Dfu(dev)
    dfu.clear_status()
    return dfu


def wait_for_device(timeout: int = 300):
    """Block until the speaker shows up in its bootloader."""
    print("Waiting for the Zeppelin Air bootloader...", end="", flush=True)
    for _ in range(timeout):
        if usb.core.find(idVendor=VID, idProduct=PID) is not None:
            print(" found")
            time.sleep(1)   # let it settle before claiming
            return open_device()
        time.sleep(1)
        print(".", end="", flush=True)
    print()
    raise RecoveryError("timed out waiting for the bootloader")


def progress(done: int, total: int, label: str) -> None:
    if total <= 0:
        return
    if done != total and done % (MAX_TRANSFER // 8) and done % 100:
        return
    pct = done * 100 // total
    print(f"\r{label}: {done}/{total} ({pct}%)", end="", flush=True)


# --------------------------------------------------------------------------
# Firmware extraction
#
# B&W's firmware is not redistributable, so it is read out of the user's own
# copy of the official recovery utility.
# --------------------------------------------------------------------------

FIRMWARE_FILES = ("dmhFixZD1d.hex", "ZD1_v2-04-05.hex", "app_DMP.bcd")

SEARCH_PATHS = (
    "/Applications/Zeppelin Air Recovery Utility.app",
    os.path.expanduser("~/Applications/Zeppelin Air Recovery Utility.app"),
    os.path.expanduser("~/Downloads"),
    "C:/Program Files/Bowers & Wilkins",
    "C:/Program Files (x86)/Bowers & Wilkins",
)


def find_firmware(explicit: str | None = None) -> dict[str, str]:
    """Locate the three firmware files, searching the usual install paths."""
    roots = [explicit] if explicit else list(SEARCH_PATHS)
    found: dict[str, str] = {}

    for root in roots:
        if not root or not os.path.exists(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name in FIRMWARE_FILES and name not in found:
                    found[name] = os.path.join(dirpath, name)
        if len(found) == len(FIRMWARE_FILES):
            break

    return found


def cmd_extract(args: argparse.Namespace) -> int:
    found = find_firmware(args.source)
    missing = [name for name in FIRMWARE_FILES if name not in found]

    if missing:
        print("Could not find: " + ", ".join(missing))
        print()
        print("Install or unpack B&W's official recovery utility, then re-run")
        print("with --source pointing at it, for example:")
        print("  ./zeppelin_recover.py extract --source ~/Downloads/ZeppelinAir")
        if not found:
            return 1

    os.makedirs(args.dest, exist_ok=True)
    for name, path in sorted(found.items()):
        target = os.path.join(args.dest, name)
        shutil.copy2(path, target)
        print(f"  {name}: {os.path.getsize(target)} bytes  <- {path}")

    print(f"\nFirmware staged in {args.dest}")
    return 0 if not missing else 1


def require_firmware(directory: str) -> dict[str, str]:
    found = {name: os.path.join(directory, name)
             for name in FIRMWARE_FILES
             if os.path.exists(os.path.join(directory, name))}
    missing = [name for name in FIRMWARE_FILES if name not in found]
    if missing:
        raise RecoveryError(
            f"missing firmware in {directory}: {', '.join(missing)}\n"
            "Run:  ./zeppelin_recover.py extract")
    return found


# --------------------------------------------------------------------------
# Recovery phases
# --------------------------------------------------------------------------

BOOTLOADER_PREP = """
  1. Unplug the mains cable.
  2. Wait for the LED to go completely black (about 10 seconds).
  3. Press and hold the Standby button.
  4. While still holding it, plug the mains cable back in.
  5. Release once the LED lights WHITE.
"""


def prompt_bootloader(step: str, assume_yes: bool = False) -> None:
    print(f"\n{step}")
    print(BOOTLOADER_PREP)
    if not assume_yes:
        input("  Press Enter once the LED is white... ")


def cmd_info(args: argparse.Namespace) -> int:
    zep = Zeppelin(open_device(), verbose=args.verbose)
    print(f"Device:             {VID:04x}:{PID:04x} (Zeppelin Air DFU)")
    print(f"Bootloader version: 0x{zep.bootloader_version():02x}")

    status = zep.dfu.get_status()
    print(f"DFU status:         {status}")
    print(f"Coprocessor state:  0x{status.coproc_state:02x}"
          f"{' (READY)' if status.coproc_state == COPROC_READY else ''}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    zep = Zeppelin(open_device(), verbose=args.verbose)
    data = zep.read_flash(args.start, args.length)
    with open(args.output, "wb") as handle:
        handle.write(data)
    print(f"Wrote {len(data)} bytes to {args.output}")
    return 0


def phase1(zep: Zeppelin, firmware: dict[str, str]) -> None:
    """Official step 1: install the DMP fix image and run it."""
    print("\n=== Phase 1: erase and install the DMP fix firmware ===")
    print("  erasing flash...")
    zep.erase()
    zep.flash_mcu(firmware["dmhFixZD1d.hex"])
    print("  starting it...")
    zep.start_application()
    print("\nPhase 1 done. The LED should now be FLASHING GREEN.")


def phase2(zep: Zeppelin, firmware: dict[str, str], verify: bool = True) -> None:
    """Official step 2: install the real MCU firmware and the coprocessor image.

    This must all happen in one session on a freshly powered up device,
    because the coprocessor init only succeeds once per power cycle.
    """
    print("\n=== Phase 2: install the application and coprocessor firmware ===")
    print("  erasing flash...")
    zep.erase()
    zep.flash_mcu(firmware["ZD1_v2-04-05.hex"])

    if verify:
        print("  verifying...")
        mismatches, compared = zep.verify_mcu(firmware["ZD1_v2-04-05.hex"])
        if mismatches:
            raise RecoveryError(f"verify failed: {mismatches} of {compared} bytes differ")
        print(f"  verified {compared} bytes, ignoring the protected "
              f"0x{PROTECTED_REGION[0]:04x}-0x{PROTECTED_REGION[1] - 1:04x} region")

    print("\n  initialising the coprocessor (this takes 20-30 seconds)...")
    zep.coproc_init()
    zep.wait_for_coproc()

    print("\n  programming the coprocessor (several minutes)...")
    zep.flash_coproc(firmware["app_DMP.bcd"])

    print("\n  starting the application...")
    zep.start_application()
    print("\nPhase 2 done. The LED should now be PULSING RED (standby).")


def cmd_recover(args: argparse.Namespace) -> int:
    firmware = require_firmware(args.firmware)

    print("Zeppelin Air recovery")
    print("=====================")
    print("\nThis erases and reflashes both the MCU and the coprocessor.")
    print("Keep the USB cable connected throughout. Mains power is cycled")
    print("between the two phases, which is required and not optional.")

    if not args.yes:
        if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    if not args.skip_phase1:
        prompt_bootloader("Phase 1 needs the speaker in its bootloader.", args.yes)
        zep = Zeppelin(wait_for_device(), verbose=args.verbose)
        phase1(zep, firmware)
        time.sleep(3)

    # A genuine power cycle here is mandatory: it is what lets the
    # coprocessor accept an init in phase 2.
    prompt_bootloader("Phase 2 needs a FRESH power cycle into the bootloader.", args.yes)
    zep = Zeppelin(wait_for_device(), verbose=args.verbose)
    phase2(zep, firmware, verify=not args.no_verify)

    print("""
Recovery complete.

Finish up on the speaker itself:
  1. Unplug mains, wait for black, plug it back in. It should come up dim red.
  2. Tap Standby to switch on. Each further tap steps to the next input:
       blue = Dock, green = USB, orange = Aux, purple = AirPlay
  3. Volume resets to zero after a reflash. Press volume + about 20 times.
     A flashing RED indicator means the volume is at its limit; once it moves
     off minimum the flash changes to the colour of the selected input.
  4. For AirPlay, reconnect it to your network: plug in ethernet, tap Standby,
     wait for solid purple, then open http://169.254.1.1 in a browser.
""")
    return 0


def cmd_phase(args: argparse.Namespace) -> int:
    firmware = require_firmware(args.firmware)
    zep = Zeppelin(open_device(), verbose=args.verbose)
    if args.which == 1:
        phase1(zep, firmware)
    else:
        phase2(zep, firmware, verify=not args.no_verify)
    return 0


def cmd_flash_coproc(args: argparse.Namespace) -> int:
    zep = Zeppelin(open_device(), verbose=args.verbose)
    zep.coproc_init()
    zep.wait_for_coproc()
    zep.flash_coproc(args.image)
    print("Coprocessor flash complete.")
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    zep = Zeppelin(open_device(), verbose=args.verbose)
    zep.start_application()
    time.sleep(3)
    if usb.core.find(idVendor=VID, idProduct=PID) is None:
        print("Application started (the device left the bootloader).")
    else:
        print("Still in the bootloader; the application did not start.")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recover a bricked Bowers & Wilkins Zeppelin Air.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Start with:  ./zeppelin_recover.py extract && ./zeppelin_recover.py recover")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log every command sent on the wire")
    parser.add_argument("-f", "--firmware", default="firmware",
                        help="directory holding the extracted firmware (default: firmware)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("extract", help="copy firmware out of the official utility")
    p.add_argument("--source", help="path to the official recovery utility")
    p.add_argument("--dest", default="firmware", help="where to stage the firmware")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("recover", help="run the full guided recovery")
    p.add_argument("-y", "--yes", action="store_true", help="skip all prompts")
    p.add_argument("--skip-phase1", action="store_true",
                   help="phase 1 is already done (LED is flashing green)")
    p.add_argument("--no-verify", action="store_true", help="skip the readback verify")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("phase", help="run a single recovery phase")
    p.add_argument("which", type=int, choices=(1, 2))
    p.add_argument("--no-verify", action="store_true")
    p.set_defaults(func=cmd_phase)

    p = sub.add_parser("info", help="show device and bootloader information")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("dump", help="read program flash to a file")
    p.add_argument("output")
    p.add_argument("--start", type=lambda v: int(v, 0), default=0)
    p.add_argument("--length", type=lambda v: int(v, 0), default=0x40000)
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("flash-coproc", help="flash only the coprocessor image")
    p.add_argument("image")
    p.set_defaults(func=cmd_flash_coproc)

    p = sub.add_parser("launch", help="start the application firmware")
    p.set_defaults(func=cmd_launch)

    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except RecoveryError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except usb.core.USBError as exc:
        print(f"\nUSB error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
