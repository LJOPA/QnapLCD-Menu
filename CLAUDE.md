# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Python scripts that drive the front-panel LCD (A125, 2 lines × 16 columns) and its two buttons on QNAP NAS hardware running **plain FreeBSD** (the user's only target on the `FreeBSD` branch; TrueNAS support is reference-only, kept in the README). Tested on a TVS-671. The scripts run on the NAS as root — they talk to a serial port and shell out to system tools (`ifconfig`, `zpool`, `uptime`).

There is no test suite or linter config. Runtime dependencies are FreeBSD packages: `pythonXY` and `pyXY-pyserial` — don't vendor pyserial or add pip-based installs for the FreeBSD path.

## Commands

```bash
python3 -m py_compile lcd-menu.py qnaplcd/__init__.py qnaplcd/web.py preinit.py postinit.py shutdown.py
sh -n rc.d/qnaplcd packaging/build-pkg.sh
cc -Wall -o /tmp/qnapled qnapled/qnapled.c   # FreeBSD only (machine/cpufunc.h)
packaging/build-pkg.sh        # on FreeBSD: dist/qnaplcd-menu-<VERSION>.pkg; elsewhere: staged tree + tarball only
sudo ./lcd-menu.py --port /dev/cuau1 --refresh 30   # run from a checkout
```

## Architecture

- **`qnaplcd/__init__.py`** — `QnapLCD` class wrapping the serial protocol (1200 baud). Commands are byte sequences prefixed with `0x4d`; responses start with `0x53` (or `0x83`). The protocol table is documented in the header comment. If a `handler` is passed, a daemon thread (`serial_reader`) blocks on reads and calls `handler(event_name, data)` for events like `'Switch_Status'` (the buttons held after every change: `0x01` = Enter, `0x02` = Select (as labelled on the TS-669), `0x03` = both, then `0x00` on release; pressing both reads e.g. `0x02 0x03 0x01 0x00`; holding sends nothing extra). `write(line, msg, echo=True)` accepts either a string or a `[line1, line2]` list; text is truncated to 16 chars; `echo=False` skips the stdout copy. If the port can't be opened it prints the error and every method becomes a no-op, while `write()` still echoes `LINE n: ...` to stdout.
- **`lcd-menu.py`** — the main program. The menu is a list of zero-arg `show_*` functions. Every `--refresh` seconds `refresh()` re-discovers network interfaces (parsed from `ifconfig -a` text; lagg member ports show link status instead of an IP), ZFS pools and pool notices, then `rebuild_menu()` rebuilds the whole list: static screens, one `show_notice` per notice (at least one), `show_updates`, one `show_ip` / `show_zpool` per item. Repeated screens work out *which* item they are with `nth()` (count of identical entries before `menu_item`) — keep that invariant. Notices are `(level, line1, line2)` with level `error` or `maint`; they set the status LED via `qnapled status-led` (red / alternate / green) and a new error jumps the menu to it. SMART checks (hourly) and update checks (`pkg`, `freebsd-update`, every 6 h) run on daemon threads. Button events arrive on the serial reader thread; they are acted on at release (`0x00`) using every bit held since the last release: Select = next screen (or "no" in a `prompt()`), Enter = `ACTIONS[screen]` (or "yes"), both = nothing. Draw screens with `show(line1, line2)`: lines longer than 16 columns are scrolled by `scroll_loop()` (both rows on one shared cycle, only while the backlight is on, `write(..., echo=False)` so the log isn't flooded) — `prompt()` word-wraps its question into the left of both lines with `Yes >` / `No >` beside Enter (top) / Select (bottom), falling back to question + `Enter=yes Sel=no` if it can't fit, so keep questions short; Enter always confirms, Select always backs out; `response_handler` swallows exceptions so a failing screen can't kill the reader thread. All display/menu state is guarded by `ui_lock` (RLock). Refresh errors are logged and retried, not fatal. SIGTERM exits via `sys.exit` so the `finally` turns the backlight off.
- **`qnapled/qnapled.c`** — the LEDs aren't on the LCD MCU. On the TS-x69 they are IT8721F Super I/O GPIOs (config port 0x2E, LDN 7), all active low; the map comes from QNAP firmware `/etc/model_3.conf` (`SIO:Inn:Bb` = set nn+1, bit b): power GP40, status green GP22 / red GP23 (red overrides green, so no amber), USB-copy LED GP50, disk 1–6 error LEDs GP84 GP83 GP21 GP20 GP70 GP87 (not yet driven), reset button GP17, USB-copy button GP34. The BIOS maps the power LED to GP LED blinker 1 (`F8 = 0x20`, location = set<<3 | bit); `power steady` unmaps it and drives GP40 as a simple-I/O output. `status-led alternate` keeps green on and gives GP23 to blinker 2 (`FA = 0x13`, `FB = 0x02`, pin not simple-I/O, like the BIOS power LED). Needs `/dev/io`, so it is C, built by `build-pkg.sh` on FreeBSD only; rc.d runs `power steady` at start when `qnaplcd_power_led` is YES, `lcd-menu.py` runs `status-led`.
- **`qnaplcd/web.py` + `qnaplcd/panel.html`** — optional web panel (`--web [ADDR:]PORT`, rc.d `qnaplcd_web`), stdlib `http.server` only: `GET /` (the page), `/api/state` (JSON), `/api/events` (SSE, one message per change, keepalive comment every 15 s), `POST /api/button/enter|select` (needs header `X-QnapLCD: 1`, so cross-site pages can't press; not auth). The display comes from `QnapLCD.shown` / `.lit`, a copy kept in `write()` / `clear()` / `backlight()` (a write overwrites the start of the row), which calls `lcd.on_change` → `web_notify()`; `set_status_led` and `handle_buttons` notify too. `web_state()` reads without `ui_lock`; `web_press()` feeds a press + release through `handle_buttons`, so web and real buttons share one path.
- **`preinit.py` / `postinit.py` / `shutdown.py`** — one-shot scripts that print a status message and exit.
- Serial port default is `qnaplcd.DEFAULT_PORT` (`/dev/cuau1`); the `QNAPLCD_PORT` env var overrides it (how the rc.d script points the one-shot scripts at `qnaplcd_port`), and `lcd-menu.py` also takes `--port`.

## Service and packaging

- `rc.d/qnaplcd` — runs `lcd-menu.py` under `daemon(8)` (`-r` restart, pidfile on the supervisor so `stop` forwards SIGTERM). `%%PREFIX%%` and `%%PYTHON%%` are substituted by the build script. `qnaplcd_flags` is folded into `command_args` and then unset, because rc.subr would otherwise put it on `daemon(8)`'s command line. On `faststop` (system shutdown) it runs `shutdown.py`.
- `packaging/build-pkg.sh` — stages into `build/pkg/stage` (installs under `/usr/local/libexec/qnaplcd`), rewrites script shebangs to the exact `/usr/local/bin/pythonX.Y` (FreeBSD has no `python3` without the metaport), and writes a `+MANIFEST` whose deps (`pythonXY`, `pyXY-pyserial`) come from the build host's Python (or `PYTHON_VERSION`) with versions from `pkg query`. Package version comes from `VERSION`.
- Local smoke test without FreeBSD/hardware: put a stub `serial` package (whose `Serial()` raises `SerialException`) on `PYTHONPATH`, run the staged `lcd-menu.py --port /dev/none --refresh 2`, and send it SIGTERM — it must exit promptly (reader thread and backlight timer are daemon threads).

`show_truenas` uses TrueNAS's `cli` when present and falls back to `freebsd-version`; `setup.sh` (vendors pyserial) exists only for TrueNAS. `lcd-menu.py.bak` is an untracked copy of the pre-port version.
