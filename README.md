> [!WARNING]
> **This fork contains AI-generated code.**
>
> The FreeBSD port, the rc.d service, the packaging script and parts of this
> README were written with the help of an AI assistant (Claude). They have been
> tested on one machine only. **Review the code before running it**, particularly
> as it runs as root.

# QnapLCD-Menu

Scripts for using the front-panel LCD and buttons on QNAP NAS hardware running
FreeBSD. They work with models that have the two-button "A125" display. The
original project was tested on a TVS-671; this fork has been tested on a
TS-669 Pro running FreeBSD 15.1.

This is a fork of [stephenhouser/QnapLCD-Menu](https://github.com/stephenhouser/QnapLCD-Menu).
This branch targets plain FreeBSD. Notes for TrueNAS CORE and SCALE are kept
at the end for reference.

## Contents

- `lcd-menu.py`: a menu similar to the stock QNAP one. It shows the host name
  and OS version, uptime, one screen per network interface and one per ZFS
  pool. The up and down buttons move between screens. The backlight turns off
  after 30 seconds without a button press.
- `preinit.py`, `postinit.py`, `shutdown.py`: short scripts that print a status
  message on the display and exit.
- `qnaplcd/`: a Python module for the A125 display. It uses pyserial and a
  background thread to pass button events to the calling program.
- `qnapled/qnapled.c`: a small C program that stops the power LED blinking on
  the TS-x69 (see "Power LED" below).
- `rc.d/qnaplcd`: an rc.d service that runs the menu under `daemon(8)`.
- `packaging/build-pkg.sh`: builds a FreeBSD package containing the above.

## Requirements

- Python 3 and pyserial, installed from FreeBSD packages
- Root access, for the serial port (`/dev/cuau1` by default)

```
pkg install python3 py311-pyserial
```

Install the `pyXY-pyserial` package that matches your Python version
(`python3 --version`).

## Installing as a service

### Building the package

On the NAS, or another FreeBSD host with the same Python version, with the
requirements installed:

```
packaging/build-pkg.sh
```

This creates `dist/qnaplcd-menu-<version>.pkg`, using the version in `VERSION`.
The package depends on the `pythonXY` and `pyXY-pyserial` packages for the
build host's Python. Set `PYTHON_VERSION` (for example `PYTHON_VERSION=3.11`)
to build for a different version.

The package installs:

- `/usr/local/libexec/qnaplcd/`: the scripts, the `qnaplcd` module and the
  `qnapled` program (built with the base system's `cc`)
- `/usr/local/etc/rc.d/qnaplcd`: the service script

### Installing and enabling

```
pkg add dist/qnaplcd-menu-1.1.0.pkg
sysrc qnaplcd_enable=YES
service qnaplcd start
```

The service restarts the menu if it exits and writes its output to
`/var/log/qnaplcd.log`. At system shutdown it shows "Shutting Down..." on the
display.

Optional `rc.conf` settings:

- `qnaplcd_port`: serial device (default `/dev/cuau1`)
- `qnaplcd_python`: Python interpreter (default: the version the package was
  built for)
- `qnaplcd_flags`: extra arguments for `lcd-menu.py`, for example
  `"--refresh 60"`
- `qnaplcd_logfile`: log file (default `/var/log/qnaplcd.log`)
- `qnaplcd_power_led`: make the power LED steady when the service starts
  (default `YES`)

### Power LED

On QNAP's own OS the power LED blinks while the NAS boots and goes steady
once it is up. Under FreeBSD nothing tells it to stop, so it blinks forever.

On the TS-x69 the LED is driven by the motherboard's IT8721F Super I/O chip
(pin GP40). The firmware connects that pin to the chip's built-in blinker.
`qnapled steady` disconnects the blinker and turns the LED on; the service
runs it at start. `qnapled blink` puts the firmware's setting back, and
`qnapled status` shows the registers. It needs root.

`qnapled` does nothing unless it finds an IT8721F whose blinker is on GP40,
so it should be harmless on other models, but it has only been tried on a
TS-669.

### Running without the package

Run the menu as root from the repository root:

```
./lcd-menu.py --port /dev/cuau1
```

## TrueNAS notes (reference only)

These are not used on this branch.

### TrueNAS CORE (FreeBSD-based)

- The display is on `/dev/cuau1`, as on plain FreeBSD, and the menu code works
  there. The first screen uses `cli -c 'system version'` if the TrueNAS `cli`
  command exists, and `freebsd-version` otherwise.
- CORE does not normally allow installing packages with `pkg`, so pyserial
  usually cannot be installed that way. Use `./setup.sh` (see below) instead.
- Each TrueNAS upgrade replaces the boot environment, which removes anything
  installed with `pkg add` and any changes to `/etc/rc.conf`. Use Tasks >
  Init/Shutdown Scripts instead: add `preinit.py` as a pre-init script,
  `lcd-menu.py` as a post-init script and `shutdown.py` as a shutdown script,
  from a copy of this repository stored on a data pool.

### TrueNAS SCALE (Linux-based)

- The upstream code targets SCALE, where the serial port is `/dev/ttyS1` and
  network details come from `ip -json`. This branch reads FreeBSD's `ifconfig`
  output instead, so use the upstream `master` branch on SCALE.
- Add `lcd-menu.py` under System Settings > Advanced > Init/Shutdown Scripts.

### Installing pyserial without pip or pkg

Run this from the repository root, the directory that contains `qnaplcd/`:

```
./setup.sh
```

It downloads pyserial's `serial/` module into the current directory, where the
scripts can import it.
