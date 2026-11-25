# QnapLCD-Menu

A simplistic package and examples using the front panel display and buttons
on QNAP NAS devices under FreeBSD. Tested with TVS-671, but should work on
other models that use the "A125" display with two buttons.

This branch targets **plain FreeBSD**. Notes for TrueNAS CORE / SCALE are kept
at the end as a reference for others.

# What's Included

* *lcd-menu.py* a menu similar to the default QNAP one: host and OS version, uptime,
  one screen per network interface and per ZFS pool, cycled with the up/down buttons.
  The backlight turns off after 30 seconds without a button press.

* *preinit.py*, *postinit.py*, *shutdown.py* short scripts that print a status
  message on the LCD panel and terminate.

* *qnaplcd* Python package (class) for using the front-panel (A125) display. Uses
  *pyserial* and threading to send button events to the calling program.

* *rc.d/qnaplcd* an `rc.d` service that runs the menu under `daemon(8)`.

* *packaging/build-pkg.sh* builds a FreeBSD package containing all of the above.

# Requirements

Python 3 and pyserial from FreeBSD packages, and root access (for the serial port,
`/dev/cuau1` by default):

```
pkg install python3 py311-pyserial
```

Use the `pyXY-pyserial` package matching your Python version (`python3 --version`).

# Installing as a service

## Building the package

On the NAS (or any FreeBSD host with the same Python version), with the
requirements above installed:

```
packaging/build-pkg.sh
```

This produces `dist/qnaplcd-menu-<version>.pkg` (version from `VERSION`), which
depends on `pythonXY` and `pyXY-pyserial` for the Python found on the build host
(override with `PYTHON_VERSION=3.11`).

The package installs:

* `/usr/local/libexec/qnaplcd/` – the scripts and the `qnaplcd` module
* `/usr/local/etc/rc.d/qnaplcd` – the service script

## Installing and enabling

```
pkg add dist/qnaplcd-menu-1.0.0.pkg
sysrc qnaplcd_enable=YES
service qnaplcd start
```

The menu is restarted if it exits, and its output goes to `/var/log/qnaplcd.log`.
At system shutdown the service shows *Shutting Down...* on the panel.

Optional `rc.conf` settings: `qnaplcd_port` (default `/dev/cuau1`),
`qnaplcd_python` (default: the Python the package was built for),
`qnaplcd_flags` (e.g. `"--refresh 60"`), `qnaplcd_logfile`.

## Running from a checkout

Without the package, run it directly as root from the repository root:

```
./lcd-menu.py --port /dev/cuau1
```

# Reference: TrueNAS notes

Not used on this branch, but kept for anyone running TrueNAS.

**TrueNAS CORE (FreeBSD-based)**

* The display is on `/dev/cuau1`, as on plain FreeBSD, and the menu code works there.
  The first screen uses TrueNAS's `cli -c 'system version'` when `cli` exists, and
  falls back to `freebsd-version` otherwise.
* CORE has no usable package repository, so `pkg install py*-pyserial` usually isn't
  possible. Use `./setup.sh` (below) to place pyserial next to `qnaplcd/` instead.
* The boot environment is replaced on every upgrade, so anything added with `pkg add`,
  and changes to `/etc/rc.conf`, are lost after updating TrueNAS. Prefer the
  *Init/Shutdown Scripts* method: under *Tasks → Init/Shutdown Scripts*, add
  `preinit.py` (pre-init), `lcd-menu.py` (post-init) and `shutdown.py` (shutdown),
  pointing at a clone of this repository on a data pool.

**TrueNAS SCALE (Linux-based)**

* The original upstream code targets SCALE: the serial port is `/dev/ttyS1` and the
  network screens came from `ip -json`. This branch parses FreeBSD's `ifconfig`
  instead, so use upstream `master` on SCALE.
* Add `lcd-menu.py` under *System Settings → Advanced → Init/Shutdown Scripts*.

**Installing pyserial without pip or pkg**

From the repository root (the *same directory* as `qnaplcd`):

```
./setup.sh
```

This downloads pyserial's `serial/` package into the current directory, where the
scripts will find it.
