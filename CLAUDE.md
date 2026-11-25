# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Python scripts that drive the front-panel LCD (A125, 2 lines × 16 columns) and its two buttons on QNAP NAS hardware running **plain FreeBSD** (the user's only target on the `FreeBSD` branch; TrueNAS support is reference-only, kept in the README). Tested on a TVS-671. The scripts run on the NAS as root — they talk to a serial port and shell out to system tools (`ifconfig`, `zpool`, `uptime`).

There is no test suite or linter config. Runtime dependencies are FreeBSD packages: `pythonXY` and `pyXY-pyserial` — don't vendor pyserial or add pip-based installs for the FreeBSD path.

## Commands

```bash
python3 -m py_compile lcd-menu.py qnaplcd/__init__.py preinit.py postinit.py shutdown.py
sh -n rc.d/qnaplcd packaging/build-pkg.sh
cc -Wall -o /tmp/qnapled qnapled/qnapled.c   # FreeBSD only (machine/cpufunc.h)
packaging/build-pkg.sh        # on FreeBSD: dist/qnaplcd-menu-<VERSION>.pkg; elsewhere: staged tree + tarball only
sudo ./lcd-menu.py --port /dev/cuau1 --refresh 30   # run from a checkout
```

## Architecture

- **`qnaplcd/__init__.py`** — `QnapLCD` class wrapping the serial protocol (1200 baud). Commands are byte sequences prefixed with `0x4d`; responses start with `0x53` (or `0x83`). The protocol table is documented in the header comment. If a `handler` is passed, a daemon thread (`serial_reader`) blocks on reads and calls `handler(event_name, data)` for events like `'Switch_Status'` (button bitmask: `0x01` = up, `0x02` = down). `write(line, msg)` accepts either a string or a `[line1, line2]` list; text is truncated to 16 chars. If the port can't be opened it prints the error and every method becomes a no-op, while `write()` still echoes `LINE n: ...` to stdout.
- **`lcd-menu.py`** — the main program. The menu is a list of zero-arg `show_*` functions; `response_handler` moves `menu_item` up/down on button presses and calls the selected function. Every `--refresh` seconds the main loop re-discovers network interfaces (parsed from `ifconfig -a` text; lagg member ports show link status instead of an IP) and ZFS pools, rebuilding the dynamic part of the menu by appending one `show_ip` / `show_zpool` entry per item. Those functions figure out *which* item they are by counting how many identical entries precede `menu_item` in `menu` — keep that invariant if you change menu construction. Refresh errors are logged and retried, not fatal. SIGTERM exits via `sys.exit` so the `finally` turns the backlight off.
- **`qnapled/qnapled.c`** — the power LED isn't on the LCD MCU. On the TS-x69 it is pin GP40 of the IT8721F Super I/O (config port 0x2E, LDN 7), active low; the BIOS maps it to GP LED blinker 1 (`F8 = 0x20`, location = set<<3 | bit). `steady` unmaps it and drives GP40 as a simple-I/O output (`C3`/`CB` bit 0). GP50 on the same chip is the blue USB-copy LED (also active low, normally 1). Needs `/dev/io`, so it is C, built by `build-pkg.sh` on FreeBSD only, and run from the rc.d `start_precmd` when `qnaplcd_power_led` is YES.
- **`preinit.py` / `postinit.py` / `shutdown.py`** — one-shot scripts that print a status message and exit.
- Serial port default is `qnaplcd.DEFAULT_PORT` (`/dev/cuau1`); the `QNAPLCD_PORT` env var overrides it (how the rc.d script points the one-shot scripts at `qnaplcd_port`), and `lcd-menu.py` also takes `--port`.

## Service and packaging

- `rc.d/qnaplcd` — runs `lcd-menu.py` under `daemon(8)` (`-r` restart, pidfile on the supervisor so `stop` forwards SIGTERM). `%%PREFIX%%` and `%%PYTHON%%` are substituted by the build script. `qnaplcd_flags` is folded into `command_args` and then unset, because rc.subr would otherwise put it on `daemon(8)`'s command line. On `faststop` (system shutdown) it runs `shutdown.py`.
- `packaging/build-pkg.sh` — stages into `build/pkg/stage` (installs under `/usr/local/libexec/qnaplcd`), rewrites script shebangs to the exact `/usr/local/bin/pythonX.Y` (FreeBSD has no `python3` without the metaport), and writes a `+MANIFEST` whose deps (`pythonXY`, `pyXY-pyserial`) come from the build host's Python (or `PYTHON_VERSION`) with versions from `pkg query`. Package version comes from `VERSION`.
- Local smoke test without FreeBSD/hardware: put a stub `serial` package (whose `Serial()` raises `SerialException`) on `PYTHONPATH`, run the staged `lcd-menu.py --port /dev/none --refresh 2`, and send it SIGTERM — it must exit promptly (reader thread and backlight timer are daemon threads).

`show_truenas` uses TrueNAS's `cli` when present and falls back to `freebsd-version`; `setup.sh` (vendors pyserial) exists only for TrueNAS. `lcd-menu.py.bak` is an untracked copy of the pre-port version.
