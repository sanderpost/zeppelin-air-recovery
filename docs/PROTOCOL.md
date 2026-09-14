# The Zeppelin Air recovery protocol

The Zeppelin Air's bootloader is an Atmel AVR32 UC3 DFU bootloader
(`03EB:2FF8`) that B&W extended with undocumented vendor commands. Those
extensions are why no off-the-shelf tool can talk to it, and why recovering one
of these speakers has a reputation for being impossible.

This document records the protocol as recovered from
`Zeppelin Air Recovery Utility.app/Contents/Resources/updater/updater.dylib`
(i386 Mach-O), cross-checked against live hardware.

## Contents

- [Transport](#transport)
- [The enable command](#1-the-enable-command)
- [Memory units](#2-memory-units)
- [MCU flash](#3-mcu-flash)
- [The coprocessor](#4-the-coprocessor)
- [Starting the application](#5-starting-the-application)
- [Recovery sequence](#6-the-full-recovery-sequence)
- [Flash layout](#7-flash-layout)
- [How this was found](#how-this-was-found)

## Transport

Standard DFU 1.1 control transfers on interface 0:

| Request | bRequest | bmRequestType | Notes |
| --- | --- | --- | --- |
| `DFU_DNLOAD` | 1 | `0x21` | `wValue` = incrementing transaction counter |
| `DFU_UPLOAD` | 2 | `0xA1` | `wValue` = transaction counter |
| `DFU_GETSTATUS` | 3 | `0xA1` | `wValue` = 0, returns 6 bytes |
| `DFU_CLRSTATUS` | 4 | `0x21` | clears a latched error |

`wIndex` is always the interface number. Every command below is a `DFU_DNLOAD`
payload followed by a `DFU_GETSTATUS`.

The 6-byte status reply is `bStatus`, `bwPollTimeout[3]`, `bState`, `iString`.
**`iString` is load-bearing on this device** — see
[the coprocessor](#4-the-coprocessor).

## 1. The enable command

```
06 06 01 00
```

This is the single most important discovery. The Zeppelin Air's bootloader
rejects *every* memory command unless this is sent first, failing with
`LIBUSB_ERROR_PIPE` and leaving stock tooling reporting
`atmel_select_memory_unit(0) failed` / `Memory access error`.

In the official updater it is `_atmel_select_ena` (`0x3710`), and every single
operation calls it with an argument of `1` before doing anything else. Stock
`dfu-programmer` has no knowledge of it, which is why it cannot read so much as
a bootloader version from this hardware. Adding it is a three-line patch and it
unlocks the whole device.

## 2. Memory units

```
06 03 00 <unit>              select memory unit          (4 bytes)
06 03 01 <page_hi> <page_lo> select 64 KiB flash page    (5 bytes)
```

Note the page select is **five** bytes, with the 16-bit page number in bytes 3
and 4. An extra padding byte is not ignored: the bootloader accepts the command,
reports `OK`, and silently stays on page 0. Reads then return page 0 mirrored
across the whole address space, and writes beyond the first 64 KiB land back in
page 0 — which corrupts any image larger than 64 KiB while appearing to
succeed. The MCU firmware spans four pages, so this matters.

The enable must not be re-sent between selecting a page and issuing the data
command, because it resets the page selection.

| Unit | Contents |
| --- | --- |
| 0 | program flash |
| 1 | EEPROM |
| 2 | security |
| 3 | configuration / fuses |
| 4 | bootloader |
| 5 | signature |
| 6 | user page |
| **7** | **coprocessor application** (B&W extension) |
| **8** | **coprocessor bootloader** (B&W extension) |
| **9** | **coprocessor extra** (B&W extension) |

Probing every unit is a useful health check: 0–6 return `OK`, 10–16 return
`errADDRESS` because they do not exist, and 7–9 return `errPROG` **until the
coprocessor has been initialised**. That last detail is the trap — `errPROG`
from units 7/8/9 reads like a hardware fault but usually just means "not booted
yet".

## 3. MCU flash

Chip erase, which answers `errNOTDONE` (`0x09`) or stalls while busy:

```
04 00 FF
```

Writes are a single `DFU_DNLOAD` of a packet laid out as:

```
+----------------------+---------------+------------------+
| control block        | data          | DFU suffix       |
| 64 + (start % 64) B  | <= 1024 bytes | 16 bytes         |
+----------------------+---------------+------------------+
```

The control block starts with a big-endian address range, and the rest is zero:

```
01 00 <start_hi> <start_lo> <end_hi> <end_lo>
```

`start` and `end` are offsets **within the selected 64 KiB page**, so a write
may never straddle a page boundary. The `start % 64` padding exists so the
payload lands on a 64-byte boundary relative to the flash address; on AVR32 the
control block is 64 bytes, against 32 on classic AVR parts.

The 16-byte suffix is a DFU file suffix with `bcdDFU` `0x0110`:

```
00 00 00 00 10 44 46 55 01 10 FF FF FF FF FF FF
             ^^ ^^^^^^^^ ^^^^^
             len 'D''F''U' bcdDFU
```

Reads are a command followed by `DFU_UPLOAD`:

```
03 00 <start_hi> <start_lo> <end_hi> <end_lo>
```

## 4. The coprocessor

The DMP coprocessor handles AirPlay, networking and audio DSP. It is a separate
processor behind the MCU, and it must be woken before it will answer.

### Init

```
06 09 <startup_hi> <startup_lo> <ready_hi> <ready_lo>
```

Two 16-bit big-endian values. The updater's own usage string gives away what
they are:

```
init-coproc [global-options] [startup timeout] [ready timeout]
```

They are **timeouts in seconds**, not addresses, and the official defaults are
hardcoded at `0x7680` as `5` and `15`:

```asm
7680:  movw  $0x5,  0x5e(%eax)    ; startup timeout
7686:  movw  $0xf,  0x60(%eax)    ; ready timeout
```

### Waiting for ready

This is the step that everything else hinges on. `init-coproc` returns `OK`
immediately; it does **not** block. The caller has to poll, and the readiness
state is reported in the `iString` byte of `DFU_GETSTATUS` — offset 5 on the
wire — which B&W repurposed for the job:

```asm
_atmel_get_coproc_status:
    call  _dfu_get_status
    movzbl -0x1b(%ebp), %eax     ; iString, not bStatus
```

`_execute_flash_coproc_from_init` (`0x18f0`) polls it once a second for up to
300 iterations:

| State | Meaning |
| --- | --- |
| `0x00`–`0x03` | starting up, keep waiting |
| `0x11` | coprocessor booting, keep waiting |
| `0x12` | **ready** |
| anything else | error, abort |

Observed on real hardware, taking 21–29 seconds:

```
t=  0s  state=0x02   starting up
t=  5s  state=0x03   waiting for coprocessor
t=  8s  state=0x11   coprocessor booting
t= 29s  state=0x12   READY
```

Only now does `06 03 00 07` return `OK`.

> **Init works once per power cycle.** Issuing `06 09` a second time without
> cutting mains power leaves the state machine stuck at `0x02` indefinitely —
> confirmed by a full 300-second stall on hardware. A real power cycle is
> required between attempts.

### Flashing

Every packet is exactly `0x450` bytes and the address is **always zero**; the
coprocessor tracks its own write position. From `_atmel_flash_coproc`
(`0x71a0`):

```asm
7270:  movl  0x10(%ebp), %eax    ; length = 0x400
7273:  xorl  %ecx, %ecx          ; start address = 0, always
727c:  calll _atmel_flash_coproc_block
```

```
+--------------+-------------+------------+
| 0x40 header  | 0x400 data  | 0x10 suffix|
+--------------+-------------+------------+
header: 01 00 00 00 03 FF
```

Blocks are always a full 1024 bytes, zero-padded at the end of the file. After
the last block the updater sleeps 2 seconds, then ends the transfer:

```
06 04 07 00      end of transfer, unit 7
06 04 08 00      unit 8
06 04 09 00      unit 9
```

`app_DMP.bcd` is 6,847,488 bytes — 6687 blocks — and is streamed raw. Its header
is `62 43 6f 44` (`bCoD`) with a build date string, but no container parsing is
needed.

## 5. Starting the application

```
06 06 01 00          enable
04 03 01 00 00       start application, no reset
<zero-length DNLOAD> manifest the jump
```

From `_atmel_start_app` (`0x4df6`). The distinction matters: the more common
`04 03 00` is a **watchdog reset**, which re-runs the bootloader, re-enters ISP
and lands straight back on a white LED. Only the no-reset form actually jumps
to the application.

## 6. The full recovery sequence

B&W's utility does this in two phases, separated by a power cycle. The exact
flow, including the easily missed repeat of the power-button preparation, is
spelled out in `MainMenu.nib`:

**Phase 1**

1. Enter the bootloader (hold Standby while inserting mains) → white LED
2. Chip erase
3. Flash `dmhFixZD1d.hex`
4. Start the application → **flashing green LED**

**Phase 2** — all in one session on a freshly powered-up device

1. Enter the bootloader again → white LED
2. Chip erase
3. Flash `ZD1_v2-04-05.hex` (0x36200 bytes)
4. `init-coproc 5 15`, then poll until `iString` is `0x12`
5. Select unit 7 and stream `app_DMP.bcd`, then end the transfer
6. Start the application → **pulsing red LED** (standby)

The USB cable stays connected throughout. Only the final success screen tells
you to remove it.

## 7. Flash layout

On the 256 KiB AT32UC3A0256, two ranges cannot be verified by readback:

| Range | Behaviour |
| --- | --- |
| `0x0000-0x1FFF` | Bootloader, BOOTPROT write-protected (`BOOTPROT` reads `0x02`). Readable, but writes are silently discarded, so it keeps the device's own bootloader rather than the code the hex files carry for this range. |
| `0x2000-0x38FF` | Second-stage bootloader that survives a chip erase. The hex files ship zeros; the device holds real data, byte-identical before and after an erase. |
| `0x3900+` | Normal application flash, verifies exactly. |

Writing across the protected range is harmless — the bootloader reports success
and silently discards it — so this tool issues the same writes the official
utility does and simply skips the range when verifying. Comparing a real
readback against `ZD1_v2-04-05.hex` gives **214,644 bytes matching with zero
mismatches** once `0x0000-0x38FF` is excluded.

## How this was found

The macOS recovery utility is x86_64, but the actual flashing logic lives in a
bundled **i386** `updater.dylib`. Rosetta 2 does not translate i386, so on
Apple Silicon the interesting code cannot be executed at all — only read.

The workflow was:

1. `objdump -d updater.dylib` for a full disassembly, then slice out individual
   functions by symbol while preserving addresses so jumps could be followed.
2. Recover argument passing: the internal helpers use `regparm(3)`, so the first
   three arguments arrive in `eax`, `edx` and `ecx` rather than on the stack,
   and position-independent code resolves string and jump-table addresses via
   `calll`/`popl %ebx` followed by `leal offset(%ebx)`.
3. Read the CLI dispatch table to enumerate the vendor commands: `erase`,
   `flash`, `flash-coproc`, `flash-coprocboot`, `flash-coprocxtra`,
   `init-coproc`, `ready-coproc`, `start`, `startBL2`, `update-coproc`.
4. Convert `MainMenu.nib` with `plutil -convert xml1` to recover the official
   user-facing procedure, which is where the second power-button preparation
   step turned up.
5. Confirm each command against live hardware, watching DFU status codes.

Two details cost the most time and are worth calling out, because both produce
symptoms that look like broken hardware:

- The `06 06 01 00` enable. Without it nothing works, and the failure mode is a
  generic USB pipe error that gives no hint that a command is missing.
- The coprocessor readiness poll. Selecting unit 7 before the coprocessor
  reports `0x12` returns `errPROG`. Because units 7, 8 and 9 all failed while
  units 0–6 answered normally, the obvious reading was a dead DMP module — the
  same conclusion a failing official utility invites. The coprocessor was
  healthy all along and simply needed 25 seconds and someone to ask politely.
