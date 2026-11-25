#!/bin/sh
#
# Build a FreeBSD package (qnaplcd-menu-<version>.pkg) from this checkout.
#
#   packaging/build-pkg.sh
#
# The package depends on the FreeBSD packages for Python and pyserial
# (e.g. python311 + py311-pyserial). Both must be installed on the build
# host, which should match the target's Python version:
#
#   pkg install python3 py311-pyserial
#
# Environment:
#   PYTHON_VERSION  Python to depend on, as "3.11" (default: the version of
#                   the build host's `python3`)
#   OUTDIR          where the .pkg goes (default: dist/)
#
# `pkg create` only exists on FreeBSD. Elsewhere the script stops after
# staging (useful for checking the layout) and writes a tarball instead.
#
set -eu

PKGNAME=qnaplcd-menu
PREFIX=/usr/local

TOP=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(cat "$TOP/VERSION")
WORK="$TOP/build/pkg"
STAGE="$WORK/stage"
OUTDIR=${OUTDIR:-$TOP/dist}
APPDIR="libexec/qnaplcd"

ON_FREEBSD=no
if [ "$(uname -s)" = FreeBSD ] && command -v pkg >/dev/null 2>&1; then
    ON_FREEBSD=yes
fi

# --- Python / dependency names ---------------------------------------------
if [ -z "${PYTHON_VERSION:-}" ]; then
    PYTHON_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
fi
PYFLAVOR="py$(echo "$PYTHON_VERSION" | tr -d .)"      # py311
PYTHON_PKG="python$(echo "$PYTHON_VERSION" | tr -d .)" # python311
PYSERIAL_PKG="${PYFLAVOR}-pyserial"                    # py311-pyserial
PYTHON_BIN="$PREFIX/bin/python$PYTHON_VERSION"         # /usr/local/bin/python3.11

dep_version() {
    if [ "$ON_FREEBSD" = yes ]; then
        v=$(pkg query '%v' "$1") || {
            echo "error: $1 is not installed on this host (pkg install $1)" >&2
            exit 1
        }
        echo "$v"
    else
        echo 0
    fi
}
PYTHON_PKG_VERSION=$(dep_version "$PYTHON_PKG")
PYSERIAL_PKG_VERSION=$(dep_version "$PYSERIAL_PKG")

# --- stage -----------------------------------------------------------------
rm -rf "$WORK"
mkdir -p "$STAGE$PREFIX/$APPDIR/qnaplcd" "$STAGE$PREFIX/etc/rc.d" "$OUTDIR"

for f in lcd-menu.py preinit.py postinit.py shutdown.py; do
    # pin the shebang: FreeBSD has no `python3` unless the python3 metaport is installed
    sed "1s|^#!.*python.*|#!$PYTHON_BIN|" "$TOP/$f" > "$STAGE$PREFIX/$APPDIR/$f"
    chmod 0755 "$STAGE$PREFIX/$APPDIR/$f"
done
for f in __init__.py web.py panel.html; do
    install -m 0644 "$TOP/qnaplcd/$f" "$STAGE$PREFIX/$APPDIR/qnaplcd/$f"
done

# power-LED helper: C, because it needs port I/O (FreeBSD's <machine/cpufunc.h>)
if [ "$ON_FREEBSD" = yes ]; then
    ${CC:-cc} -O2 -Wall -o "$STAGE$PREFIX/$APPDIR/qnapled" "$TOP/qnapled/qnapled.c"
    chmod 0755 "$STAGE$PREFIX/$APPDIR/qnapled"
fi

sed -e "s|%%PREFIX%%|$PREFIX|g" -e "s|%%PYTHON%%|$PYTHON_BIN|g" \
    "$TOP/rc.d/qnaplcd" > "$STAGE$PREFIX/etc/rc.d/qnaplcd"
chmod 0755 "$STAGE$PREFIX/etc/rc.d/qnaplcd"

# --- packing list (paths relative to PREFIX) -------------------------------
PLIST="$WORK/plist"
# files are staged as the building user; install them as root:wheel regardless
{
    echo "@owner root"
    echo "@group wheel"
    (cd "$STAGE$PREFIX" && find . -type f | sed 's|^\./||' | sort)
} > "$PLIST"

# --- manifest ----------------------------------------------------------------
cat > "$WORK/+MANIFEST" <<MANIFEST
name: $PKGNAME
version: "$VERSION"
origin: sysutils/$PKGNAME
comment: "Menu for the QNAP A125 front-panel LCD and buttons"
www: "https://github.com/stephenhouser/QnapLCD-Menu"
maintainer: "root@localhost"
prefix: "$PREFIX"
licenselogic: single
licenses: [ MIT ]
categories: [ sysutils ]
deps: {
  $PYTHON_PKG: { origin: "lang/$PYTHON_PKG", version: "$PYTHON_PKG_VERSION" },
  $PYSERIAL_PKG: { origin: "comms/py-pyserial", version: "$PYSERIAL_PKG_VERSION" }
}
desc: <<EOD
Drives the 2x16 front-panel LCD and its two buttons on QNAP NAS hardware
running FreeBSD: host, OS version, uptime, IP addresses and ZFS pool usage,
notices and updates, cycled with the Select button. On the TS-x69 it also
stops the power LED blinking and shows warnings on the status LED.
Optionally serves a live web view of the panel for remote use.
EOD
messages: [
  { message: <<EOM
To start the LCD menu now and at every boot:

  sysrc qnaplcd_enable=YES
  service qnaplcd start

To use the panel from a browser (no authentication - pick the address):

  sysrc qnaplcd_web=8080

Settings (qnaplcd_port, qnaplcd_python, qnaplcd_flags, qnaplcd_logfile,
qnaplcd_power_led, qnaplcd_web) are documented at the top of $PREFIX/etc/rc.d/qnaplcd.
EOM
  }
]
MANIFEST

if [ "$ON_FREEBSD" = yes ]; then
    pkg create -M "$WORK/+MANIFEST" -p "$PLIST" -r "$STAGE" -o "$OUTDIR"
    echo "built: $OUTDIR/$PKGNAME-$VERSION.pkg (depends on $PYTHON_PKG, $PYSERIAL_PKG)"
else
    tar -czf "$OUTDIR/$PKGNAME-$VERSION-stage.tar.gz" -C "$STAGE" .
    echo "not on FreeBSD: skipped pkg create and the qnapled build (dependency versions in the manifest are placeholders)"
    echo "staged tree: $STAGE"
    echo "tarball:     $OUTDIR/$PKGNAME-$VERSION-stage.tar.gz"
fi
