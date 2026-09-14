# Zeppelin Air Recovery

Recover a bricked Bowers & Wilkins Zeppelin Air over USB, on any modern machine.

If your Zeppelin Air shows a **solid white LED** and refuses to play anything, its
Atmel AVR32 MCU is stuck in a DFU bootloader after a failed firmware update. B&W
shipped a recovery utility for this, but it has two problems:

- On macOS it is a 32-bit i386 binary, which **cannot run at all** on any Mac
  since Catalina. Rosetta 2 translates x86_64 only, not i386.
- Its second step fails on many units with *"Could not restore the Zeppelin
  Air..."*, leaving the speaker just as dead as before.

This tool reimplements the entire procedure in one Python script, including the
undocumented protocol extensions B&W added to the stock Atmel bootloader, and it
fixes the timing bug that makes step 2 fail.

> **Why step 2 fails.** The DMP coprocessor must be explicitly powered up and
> then *polled until it reports ready*, which takes 20–30 seconds. Until then it
> answers every request with `errPROG`, which looks exactly like dead hardware.
> Tools that select the coprocessor immediately — or give up too early — fail
> here, and the speaker gets misdiagnosed as having a faulty DMP module.

## Requirements

- Python 3.9+ and [pyusb](https://github.com/pyusb/pyusb)
- libusb (`brew install libusb` on macOS, usually already present on Linux)
- A USB cable to the speaker's rear USB port
- B&W's official recovery utility, for the firmware images only

## Install

```sh
git clone https://github.com/sanderpost/zeppelin-air-recovery.git
cd zeppelin-air-recovery
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

## Firmware

The firmware belongs to B&W and is **not** included here. Install or unpack
their recovery utility, then stage the images locally:

```sh
./zeppelin_recover.py extract
```

This searches the usual install locations for the three files it needs —
`dmhFixZD1d.hex`, `ZD1_v2-04-05.hex` and `app_DMP.bcd` — and copies them into
`firmware/`. Point it somewhere explicit if they live elsewhere:

```sh
./zeppelin_recover.py extract --source ~/Downloads/ZeppelinAir
```

## Recover

```sh
./zeppelin_recover.py recover
```

The script walks you through both phases and prompts for the physical steps it
cannot perform itself. Expect it to take about five minutes, most of that
streaming 6.8 MB to the coprocessor.

To get the speaker into its bootloader when asked:

1. Unplug the mains cable.
2. Wait for the LED to go completely black, about 10 seconds.
3. Press and hold the **Standby** button.
4. While still holding it, plug the mains cable back in.
5. Release once the LED is **white**.

Keep the USB cable connected the whole way through. The mains power cycle
between the two phases is **mandatory**, not a formality — see the caveat below.

### What each phase does

| Phase | Actions | LED when finished |
| --- | --- | --- |
| 1 | Chip erase, flash `dmhFixZD1d.hex`, start it | Flashing green |
| 2 | Chip erase, flash `ZD1_v2-04-05.hex`, verify, init + flash the coprocessor, start | Pulsing red |

Pulsing red means standby, and the recovery worked.

## After recovery

The firmware reflash resets the speaker's settings, so a working speaker can
still look broken at first. In particular **the volume is reset to zero**.

1. Power cycle it once. It should come up dim red.
2. Tap **Standby** to switch on. Each further tap steps to the next connected
   input. Press duration matters: a brief tap switches on or changes input, a
   2-second hold means sleep, and a 4-second hold means standby.
3. Press volume **+** about 20 times.

The LED tells you exactly what state it is in:

| Indicator | Meaning |
| --- | --- |
| Dim red | Standby |
| Bright red | Sleep |
| Blue | On, Dock |
| Green | On, USB |
| Orange | On, Aux |
| Purple | On, AirPlay |
| Slow flashing purple | On, AirPlay, no network configured |
| Fast flashing red | **Volume at minimum or maximum** |
| Fast flashing (input colour) | Volume adjusting |
| White | Firmware update / bootloader |
| Flashing white | Firmware error |

A **fast flashing red** LED when you press volume means the volume is pinned at
one end of its range, which after a reflash means zero. Keep pressing volume `+`
until the flash changes to the colour of the selected input.

Wi-Fi credentials are also wiped, so AirPlay will show slow flashing purple
until you reconnect it: plug in ethernet, tap Standby, wait for solid purple,
then open <http://169.254.1.1> and enter your network details.

## Other commands

```sh
./zeppelin_recover.py info                  # bootloader version, coprocessor state
./zeppelin_recover.py dump flash.bin        # back up the MCU flash first
./zeppelin_recover.py phase 1               # run a single phase
./zeppelin_recover.py phase 2 --no-verify
./zeppelin_recover.py flash-coproc app_DMP.bcd
./zeppelin_recover.py launch                # start the app without reflashing
./zeppelin_recover.py -v info               # log every byte on the wire
```

Taking a backup before you start is a good idea:

```sh
./zeppelin_recover.py dump zeppelin-backup.bin
```

## Caveats

**The coprocessor accepts an init only once per power cycle.** A second attempt
without cutting mains power leaves its state machine stuck at `0x02` forever.
This is why `recover` insists on a real power cycle between phases, and why the
script aborts with a clear message after 60 seconds of no progress rather than
waiting out the full 300-second timeout.

**Flash below `0x3900` cannot be verified.** `0x0000-0x1FFF` is the bootloader,
write-protected by the BOOTPROT fuse and read back as `0xFF`. `0x2000-0x38FF`
holds a second-stage bootloader that survives a chip erase, where the hex files
ship zeros but the device holds real data. Writes are still issued across this
range, exactly as the official utility does, but verification skips it. This is
the same benign mismatch `dfu-programmer` reports as *"5841 invalid bytes"*.

**Audio output is unverified.** The firmware recovery itself is confirmed: all
6687 coprocessor blocks accepted, MCU flash verified byte-exact by readback, and
the restored firmware boots to standby with working volume and input switching.
The speaker this was developed against was never confirmed to produce sound, so
that last link in the chain is untested. If it works for you, please say so in
an issue.

**`ISP_FORCE` may remain set.** On the development unit, `setfuse ISP_FORCE 0`
reported success but read back unchanged, so the speaker needed its Standby
button held to power on. Harmless, but worth knowing.

## Testing

The protocol logic is tested without a device, and the Intel HEX parser is
validated against a real 256 KiB flash readback:

```sh
./.venv/bin/python tests/test_protocol.py
```

## How it works

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the wire protocol, the undocumented
B&W commands, and how they were recovered.

## Licence

MIT, see [LICENSE](LICENSE). No B&W firmware or code is included or
redistributed. Not affiliated with or endorsed by Bowers & Wilkins.
