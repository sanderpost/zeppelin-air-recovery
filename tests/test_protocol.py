"""Offline tests for the Zeppelin Air recovery protocol.

These run without a device. A fake DFU transport records every packet so the
exact bytes put on the wire can be asserted, which is the part that cannot be
checked against a flash dump.

Run with pytest, or directly:  python tests/test_protocol.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from zeppelin_recover import (  # noqa: E402
    CONTROL_BLOCK,
    COPROC_READY,
    DFU_SUFFIX,
    FOOTER,
    MAX_TRANSFER,
    PAGE_SIZE,
    UNIT_COPROC,
    DfuStatus,
    RecoveryError,
    Zeppelin,
    contiguous_runs,
    parse_ihex,
)


class FakeDfu:
    """Records downloads and always reports success."""

    def __init__(self, coproc_states: list[int] | None = None):
        self.sent: list[bytes] = []
        self.transaction = 0
        self.coproc_states = coproc_states or [COPROC_READY]
        self._index = 0

    def download(self, data: bytes) -> int:
        self.sent.append(bytes(data))
        return len(data)

    def upload(self, length: int) -> bytes:
        return bytes(length)

    def get_status(self) -> DfuStatus:
        state = self.coproc_states[min(self._index, len(self.coproc_states) - 1)]
        self._index += 1
        return DfuStatus(bytes([0x00, 0, 0, 0, 0x02, state]))

    def clear_status(self) -> None:
        pass

    def commands(self, prefix: bytes) -> list[bytes]:
        return [p for p in self.sent if p.startswith(prefix)]

    @property
    def writes(self) -> list[bytes]:
        """Packets that are memory writes rather than 4-6 byte commands."""
        return [p for p in self.sent if len(p) > 16]


def write_hex(records: list[str]) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".hex", delete=False)
    handle.write("\n".join(records) + "\n")
    handle.close()
    return handle.name


def ihex_record(addr: int, data: bytes, rectype: int = 0x00) -> str:
    body = [len(data), (addr >> 8) & 0xFF, addr & 0xFF, rectype, *data]
    checksum = (-sum(body)) & 0xFF
    return ":" + bytes([*body, checksum]).hex().upper()


# --------------------------------------------------------------------------
# Intel HEX
# --------------------------------------------------------------------------

def test_parse_simple_records():
    path = write_hex([ihex_record(0x0000, b"\x01\x02\x03\x04"),
                      ihex_record(0x0004, b"\xAA\xBB"),
                      ":00000001FF"])
    memory = parse_ihex(path)
    os.unlink(path)
    assert memory == {0: 1, 1: 2, 2: 3, 3: 4, 4: 0xAA, 5: 0xBB}


def test_extended_linear_address_and_avr32_base():
    """AVR32 images are based at 0x80000000, which must map to flash offset 0."""
    path = write_hex([ihex_record(0x0000, b"\x80\x00", 0x04),   # base 0x80000000
                      ihex_record(0x2000, b"\xDE\xAD"),
                      ":00000001FF"])
    memory = parse_ihex(path)
    os.unlink(path)
    assert memory == {0x2000: 0xDE, 0x2001: 0xAD}


def test_bad_checksum_is_rejected():
    path = write_hex([":0400000001020304FF"])
    try:
        parse_ihex(path)
    except RecoveryError as exc:
        assert "checksum" in str(exc)
    else:
        raise AssertionError("bad checksum accepted")
    finally:
        os.unlink(path)


def test_contiguous_runs_splits_on_gaps():
    memory = {0: 1, 1: 2, 5: 3, 6: 4}
    assert contiguous_runs(memory) == [(0, b"\x01\x02"), (5, b"\x03\x04")]


# --------------------------------------------------------------------------
# MCU write packets
# --------------------------------------------------------------------------

def test_mcu_write_packet_geometry():
    dfu = FakeDfu()
    zep = Zeppelin(dfu)
    zep._write_block(0x2000, b"\xAA" * MAX_TRANSFER)

    packet = dfu.writes[0]
    assert len(packet) == CONTROL_BLOCK + MAX_TRANSFER + FOOTER
    # header: 01 00 <start be16> <end be16>
    assert packet[:6] == bytes([0x01, 0x00, 0x20, 0x00, 0x23, 0xFF])
    assert packet[CONTROL_BLOCK:CONTROL_BLOCK + MAX_TRANSFER] == b"\xAA" * MAX_TRANSFER
    assert packet[-FOOTER:] == DFU_SUFFIX


def test_mcu_write_alignment_padding():
    """Unaligned starts shift the data by start % 64 so it lands on a boundary."""
    dfu = FakeDfu()
    zep = Zeppelin(dfu)
    zep._write_block(0x2010, b"\xBB" * 16)

    packet = dfu.writes[0]
    alignment = 0x2010 % CONTROL_BLOCK
    assert alignment == 0x10
    assert len(packet) == CONTROL_BLOCK + alignment + 16 + FOOTER
    assert packet[:6] == bytes([0x01, 0x00, 0x20, 0x10, 0x20, 0x1F])
    offset = CONTROL_BLOCK + alignment
    assert packet[offset:offset + 16] == b"\xBB" * 16


def test_flash_mcu_respects_page_and_transfer_limits():
    """No block may exceed 1024 bytes or straddle a 64 KiB page boundary."""
    size = PAGE_SIZE + 0x800          # deliberately crosses into page 1
    records = [ihex_record(0x0000, b"\x80\x00", 0x04)]
    for offset in range(0, size, 32):
        records.append(ihex_record(offset & 0xFFFF, b"\xCD" * 32))
        if (offset & 0xFFFF) == 0xFFE0:   # next record rolls into a new page
            records.append(ihex_record(0x0001, b"\x80\x01", 0x04))
    records.append(":00000001FF")

    path = write_hex(records)
    dfu = FakeDfu()
    zep = Zeppelin(dfu)
    written = zep.flash_mcu(path)
    os.unlink(path)

    assert written == size

    for packet in dfu.writes:
        start = (packet[2] << 8) | packet[3]
        end = (packet[4] << 8) | packet[5]
        length = end - start + 1
        assert length <= MAX_TRANSFER, f"block of {length} bytes exceeds the limit"
        assert start + length <= PAGE_SIZE, "block crosses a page boundary"

    # Both pages must have been selected.
    selects = dfu.commands(bytes([0x06, 0x03, 0x01]))
    pages = {(cmd[4] << 8) | cmd[5] for cmd in selects}
    assert pages == {0, 1}, f"expected pages 0 and 1, got {pages}"


# --------------------------------------------------------------------------
# Coprocessor
# --------------------------------------------------------------------------

def test_coproc_packets_always_address_zero():
    import time as time_module
    original = time_module.sleep
    time_module.sleep = lambda _seconds: None
    try:
        blob = tempfile.NamedTemporaryFile(suffix=".bcd", delete=False)
        blob.write(b"\xEE" * (MAX_TRANSFER * 2 + 10))   # tail forces zero padding
        blob.close()

        dfu = FakeDfu()
        zep = Zeppelin(dfu)
        zep.flash_coproc(blob.name, unit=UNIT_COPROC)
        os.unlink(blob.name)
    finally:
        time_module.sleep = original

    assert dfu.commands(bytes([0x06, 0x03, 0x00, UNIT_COPROC]))
    assert dfu.commands(bytes([0x06, 0x04, UNIT_COPROC, 0x00]))

    packets = dfu.writes
    assert len(packets) == 3, f"expected 3 blocks, got {len(packets)}"
    for packet in packets:
        assert len(packet) == CONTROL_BLOCK + MAX_TRANSFER + FOOTER
        # Address is always zero; the coprocessor tracks its own position.
        assert packet[:6] == bytes([0x01, 0x00, 0x00, 0x00, 0x03, 0xFF])
        assert packet[-FOOTER:] == DFU_SUFFIX

    # The short final block must be zero padded to a full 1024 bytes.
    tail = packets[-1][CONTROL_BLOCK:CONTROL_BLOCK + MAX_TRANSFER]
    assert tail[:10] == b"\xEE" * 10
    assert tail[10:] == bytes(MAX_TRANSFER - 10)


def test_coproc_init_encodes_timeouts_big_endian():
    dfu = FakeDfu()
    Zeppelin(dfu).coproc_init(startup=5, ready=15)
    assert dfu.commands(bytes([0x06, 0x09]))[0] == bytes([0x06, 0x09, 0, 5, 0, 15])


def test_wait_for_coproc_accepts_boot_sequence():
    import time as time_module
    original = time_module.sleep
    time_module.sleep = lambda _seconds: None
    try:
        # The real boot sequence observed on hardware.
        dfu = FakeDfu([0x02, 0x02, 0x03, 0x11, 0x11, COPROC_READY])
        Zeppelin(dfu).wait_for_coproc(timeout=30)
    finally:
        time_module.sleep = original


def test_wait_for_coproc_rejects_error_state():
    import time as time_module
    original = time_module.sleep
    time_module.sleep = lambda _seconds: None
    try:
        dfu = FakeDfu([0x02, 0x06])      # 0x06 is not a documented wait state
        try:
            Zeppelin(dfu).wait_for_coproc(timeout=30)
        except RecoveryError as exc:
            assert "0x06" in str(exc)
        else:
            raise AssertionError("error state accepted")
    finally:
        time_module.sleep = original


def test_select_ena_is_sent_before_memory_unit():
    """Every memory operation must be preceded by B&W's enable command."""
    dfu = FakeDfu()
    Zeppelin(dfu).select_memory_unit(0)
    assert dfu.sent[0] == bytes([0x06, 0x06, 0x01, 0x00])
    assert dfu.sent[1] == bytes([0x06, 0x03, 0x00, 0x00])


def test_start_application_uses_no_reset_sequence():
    """A watchdog reset would re-enter the bootloader instead of the app."""
    dfu = FakeDfu()
    Zeppelin(dfu).start_application()
    assert bytes([0x04, 0x03, 0x01, 0x00, 0x00]) in dfu.sent
    assert bytes([0x04, 0x03, 0x00]) not in dfu.sent


# --------------------------------------------------------------------------

def _run() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0

    devnull = open(os.devnull, "w")
    for name, fn in tests:
        stdout = sys.stdout
        sys.stdout = devnull            # the protocol code prints progress
        try:
            fn()
            sys.stdout = stdout
            print(f"  PASS  {name}")
        except Exception as exc:        # noqa: BLE001 - test harness
            sys.stdout = stdout
            print(f"  FAIL  {name}: {exc}")
            failures += 1
    devnull.close()

    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
