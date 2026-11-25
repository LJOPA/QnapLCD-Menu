# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Python scripts that drive the front-panel LCD (A125, 2 lines × 16 columns) and its two buttons on QNAP NAS hardware running a non-QNAP OS (TrueNAS / FreeBSD). Tested on a TVS-671. The scripts run **on the NAS as root** — they talk to a serial port and shell out to system tools, so they cannot be meaningfully run on a dev machine.

There is no build system, test suite, linter config, or package metadata. The only dependency is `pyserial`.

## Running / installing (on the NAS)

```bash
pip3 install pyserial     # or, where pip is unavailable:
./setup.sh                # vendors pyserial's `serial/` package into the repo root
sudo ./lcd-menu.py        # long-running menu; registered as an Init/Shutdown script in TrueNAS
```

`setup.sh` must be run from the repo root so `serial/` sits next to `qnaplcd/` and is importable.

Quick syntax check without hardware: `python3 -m py_compile lcd-menu.py qnaplcd/__init__.py`.
If the serial port can't be opened, `QnapLCD` prints the error and every method becomes a no-op, while `write()` still echoes `LINE n: ...` to stdout — useful for eyeballing menu output, though `lcd-menu.py` will still need the platform commands below to exist.

## Architecture

- **`qnaplcd/__init__.py`** — `QnapLCD` class wrapping the serial protocol (1200 baud). Commands are byte sequences prefixed with `0x4d`; responses start with `0x53` (or `0x83`). The protocol table is documented in the header comment. If a `handler` is passed, a background thread (`serial_reader`) blocks on reads and calls `handler(event_name, data)` for events like `'Switch_Status'` (button bitmask: `0x01` = up, `0x02` = down). `write(line, msg)` accepts either a string or a `[line1, line2]` list; text is truncated to 16 chars.
- **`lcd-menu.py`** — the main program. The menu is a list of zero-arg `show_*` functions; `response_handler` moves `menu_item` up/down on button presses and calls the selected function. Every 30 s the main loop re-discovers network interfaces and ZFS pools and rebuilds the dynamic part of the menu by appending one `show_ip` / `show_zpool` entry per item. Those functions figure out *which* item they are by counting how many identical entries precede `menu_item` in `menu` — keep that invariant if you change menu construction. A `threading.Timer` turns the backlight off after `DISPLAY_TIMEOUT`.
- **`preinit.py` / `postinit.py` / `shutdown.py`** — one-shot scripts that print a status message and exit; meant for TrueNAS pre-init / post-init / shutdown hooks. `postinit.py` is normally replaced by `lcd-menu.py`.

## Platform notes

The code was originally written for TrueNAS SCALE (Linux, `/dev/ttyS1`, `ip -json`). The current working tree targets **FreeBSD / TrueNAS CORE**: serial port `/dev/cuau1`, interfaces parsed from `ifconfig -a` text (lagg member ports show link status instead of an IP), and `show_truenas` falls back to `freebsd-version` when the TrueNAS `cli` binary is absent. The README still describes the SCALE setup. `lcd-menu.py.bak` is an untracked copy of the older version.

The serial port default is duplicated: `PORT` in `lcd-menu.py` and the `port=` default in `QnapLCD.__init__` (used by the one-shot scripts) — change both together.
