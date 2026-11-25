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
  and OS version, uptime, notices (see below), available updates, one screen
  per network interface and one per ZFS pool. Select moves to the next
  screen; Enter runs the screen's action, where it has one (Updates). Lines
  too long for the 16-column display scroll. The backlight turns off after
  30 seconds without a button press. Optionally it serves a web page showing
  the display, LEDs and buttons (see "Web panel" below).
- `preinit.py`, `postinit.py`, `shutdown.py`: short scripts that print a status
  message on the display and exit.
- `qnaplcd/`: a Python module for the A125 display. It uses pyserial and a
  background thread to pass button events to the calling program.
- `qnapled/qnapled.c`: a small C program that drives the TS-x69's power and
  status LEDs (see "LEDs" below).
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
pkg add dist/qnaplcd-menu-1.3.0.pkg
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
  `"--refresh 60"`. `--no-status-led` leaves the status LED alone and
  `--no-updates` removes the Updates screen.
- `qnaplcd_logfile`: log file (default `/var/log/qnaplcd.log`)
- `qnaplcd_power_led`: make the power LED steady when the service starts
  (default `YES`)
- `qnaplcd_web`: serve the web panel on `[ADDRESS:]PORT`, for example `8080`
  or `192.168.1.10:8080` (default: off)

### Web panel

With `qnaplcd_web` set (or `--web` on the command line), the menu serves a
page that shows the display, the status and LAN lights, two lights for each
drive bay and the two buttons, and updates as they change. Clicking a button
(or pressing Space for Select, Enter for Enter) works exactly like pressing
the real one, including turning the backlight on.

- Each bay has a health strip (green with a disk, red when a notice reports
  errors for that disk, off when empty) and beside it an activity light that
  glows dim green while the disk is busy. Under each bay: its number, disk
  and the name ZFS knows it by (from `zpool status`, e.g. `DISK-5YD7DCKZ`). Bays 1-6 are
  AHCI channels 0-5 (the TS-x69's three Marvell controllers), checked on a
  TS-669.
- The LAN light is amber while a network port has a link and dims while
  there is traffic over 4 KB/s (the page's own updates stay below that).
- Where the NAS has its power strip, the page shows its own connection
  instead, labelled "web link": green while it is getting updates, red if
  the connection to the NAS is lost (the buttons are disabled until it is
  back).
- USB and eSATA are drawn but not reported.

Disk and network activity are checked once a second, and the disks in the
bays every 30 seconds, only while a page is open. With no page open the web
panel runs nothing in the background.

```
sysrc qnaplcd_web=8080
service qnaplcd restart
```

Then open `http://<nas>:8080/`. The page needs no internet access.

**There is no login.** Anyone who can reach the port can use the buttons,
which includes installing updates and rebooting. Bind it to an address only
trusted machines can reach (`127.0.0.1:8080` plus an SSH tunnel, or a
management network), or put it behind a reverse proxy with authentication.

### Notices

The Notices screens list anything worth a look, and the status LED shows
the worst of them:

| Status LED | Meaning | Notices |
|---|---|---|
| steady green | all fine | none |
| alternating red/green | routine maintenance | a pool scrubbing or resilvering (with % done) |
| steady red | errors | a pool not ONLINE, read/write/checksum errors on a disk, data errors, or a SMART problem (failed health check, reallocated, pending or uncorrectable sectors) |

A new error also turns the backlight on and jumps to its notice. Pools are
checked every refresh; SMART (needs `smartmontools`) once an hour.

### Updates

The Updates screen shows how many package updates (`pkg`) and whether base
system updates (`freebsd-update`) are waiting. It checks a minute after
start and then every six hours. To install, go to the Updates screen and
press Enter. Questions are labelled beside the buttons (Enter is beside the
top line, Select beside the bottom one): Enter always confirms and Select
backs out, as does waiting 30 seconds.

```
|Install    Yes >|   Enter
|updates?    No >|   Select
```

If the update changed the kernel, the panel then asks whether to reboot;
until the NAS restarts, the Updates screen shows "Reboot needed" and Enter
asks again. With nothing to install, Enter checks again now. Checking takes
a few minutes on the TS-x69's Atom (`freebsd-update fetch` reads every
base-system file); the screen keeps the last result meanwhile. Output goes
to the log file.

### LEDs

On the TS-x69 the front-panel LEDs are driven by the motherboard's IT8721F
Super I/O chip. The pin for each LED comes from QNAP's own firmware
(`/etc/model_3.conf` for the TS-669 Pro); each LED is on when its pin is low.

- Power LED (GP40): QNAP's OS stops it blinking once the NAS is up; under
  FreeBSD nothing does, so it blinks forever. `qnapled power steady`
  disconnects the chip's built-in blinker and turns the LED on; the service
  runs it at start. `qnapled power blink` puts the firmware's setting back.
- Status LED, green (GP22) and red (GP23): red wins when both are on, so
  there is no amber. `qnapled status-led green|red|alternate|off`; for
  `alternate` green stays on and the chip's second blinker switches red.

`qnapled status` shows the registers. It needs root. It changes nothing
unless it finds an IT8721F with the pins set up as expected, so it should be
harmless on other models, but it has only been tried on a TS-669.

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
