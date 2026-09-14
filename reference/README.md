# Reference C tools

These are the standalone libusb tools that actually performed the successful
recovery, kept here because they are the proven path and because they were
originally written in `/tmp`, which macOS clears on reboot.

`zeppelin_recover.py` reimplements what they do; these remain the reference for
the exact byte sequences that are known to work on hardware.

Build:

```sh
cc -o zepflash zepflash.c $(pkg-config --cflags --libs libusb-1.0)
cc -o zepwait  zepwait.c  $(pkg-config --cflags --libs libusb-1.0)
```

- `zepwait` — initialises the coprocessor and polls until it reports ready
  (state `0x12`), then selects its memory unit. Useful on its own to confirm
  the coprocessor is alive.
- `zepflash` — the full coprocessor flash: init if needed, wait for ready,
  select the unit, stream all 6687 blocks, then end the transfer. Skips the
  init when the coprocessor is already ready.

```sh
./zepflash "/Applications/Zeppelin Air Recovery Utility.app/Contents/Resources/updater/app_DMP.bcd"
```

Follow it immediately, in the same power cycle, with the application start:

```sh
dfu-programmer at32uc3a0256 launch --no-reset
```

That `dfu-programmer` has to be the patched build that sends the `06 06 01 00`
enable command before every memory operation; an unpatched one fails with a
pipe error. See `docs/PROTOCOL.md`.
