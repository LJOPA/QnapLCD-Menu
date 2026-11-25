#!/usr/bin/env python3
import sys
import os
import time
import qnaplcd
import platform
import subprocess
import threading
import re
import shutil
import signal
import argparse
import shlex
from collections import namedtuple
from functools import partial

DISPLAY_TIMEOUT = 30    # seconds
OVERVIEW_TIMEOUT = 120    # the overview stays lit longer
ALERT_LOOPS = 2         # times a new notice scrolls by before the panel sleeps
PROMPT_TIMEOUT = 30     # seconds before an unanswered prompt cancels itself
SMART_INTERVAL = 3600   # seconds between SMART checks
UPDATE_INTERVAL = 6 * 3600  # seconds between update checks
SCROLL_STEP = 0.6       # seconds per character when a line scrolls
SCROLL_PAUSE = 2.0      # seconds to hold the start and the end of a long line

PORT = os.environ.get('QNAPLCD_PORT', qnaplcd.DEFAULT_PORT)
PORT_SPEED = 1200
HERE = os.path.dirname(os.path.abspath(__file__))
QNAPLED = os.path.join(HERE, 'qnapled')

lcd = None
args = None

# Everything that touches the display or the menu holds this lock: button
# events arrive on the serial reader thread, refreshes on the main thread,
# and update checks/installs on their own threads.
ui_lock = threading.RLock()

lcd_timer = None
timer_gen = 0       # bumped by lcd_on(), so a timer that fired late does nothing
backlight_on = False

def lcd_off(gen=None):
    """Backlight off, and back to the overview for the next wake-up. The
    screen is blanked too, and stays blank (nothing is drawn, so the web
    panel shows it blank as well) until it wakes."""
    global backlight_on, prompt_state
    with ui_lock:
        if gen is not None and gen != timer_gen:
            return
        if prompt_state is not None:
            prompt_state = None
            if prompt_timer:
                prompt_timer.cancel()
        go_home()
        backlight_on = False
        lcd.backlight(False)
        redraw()
        lcd.clear()

def display_active():
    """Draw only while the backlight is on."""
    return backlight_on

def lcd_on():
    """Backlight on, and (re)start the timer that turns it off: longer on
    the overview, none while an alert is up (it ends itself)."""
    global lcd_timer, backlight_on, timer_gen
    with ui_lock:
        lcd.backlight(True)
        backlight_on = True

        if lcd_timer:
            lcd_timer.cancel()
        timer_gen += 1
        lcd_timer = None
        if alert:
            return

        timeout = OVERVIEW_TIMEOUT if prompt_state is None and at_overview() else DISPLAY_TIMEOUT
        lcd_timer = threading.Timer(timeout, lcd_off, args=(timer_gen,))
        lcd_timer.daemon = True
        lcd_timer.start()

def shell(cmd):
    return subprocess.check_output(cmd, shell=True, universal_newlines=True).strip()

def run(cmd, timeout=None, log=True):
    """Run a command; returns (exit code, output). log=False keeps its
    output out of the log (routine checks whose callers log a summary)."""
    env = dict(os.environ, PAGER='cat', ASSUME_ALWAYS_YES='yes')
    p = subprocess.run(cmd, shell=True, universal_newlines=True, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    if not log:
        return p.returncode, p.stdout
    # freebsd-update can list thousands of files; keep the log readable
    lines = p.stdout.rstrip('\n').split('\n')
    if len(lines) > 40:
        lines = lines[:20] + [f'... ({len(lines) - 40} lines skipped) ...'] + lines[-20:]
    print(f'$ {cmd} -> {p.returncode}', *lines, sep='\n', flush=True)
    return p.returncode, p.stdout

COLUMNS = 16

# What is on the display. Lines longer than COLUMNS scroll: scroll_loop()
# holds the start for SCROLL_PAUSE, steps along one character at a time,
# holds the end, then starts over. A ticker line (show(..., ticker=True),
# line 2 of the status and alert screens) instead runs round and round,
# TICKER_SEP between the end and the start. A screen can also swap line 1
# for another after TITLE_TIME (the overview: its name, then the host). Only
# while the backlight is on - at 1200 baud each step costs ~0.2 s of serial
# time.
TICKER_SEP = '  |  '
TITLE_TIME = 3.0    # seconds before line 1 changes to `later`
display = ['', '']
title = None        # line 1 as drawn, before any swap
title_later = None  # what line 1 becomes after TITLE_TIME
drawn_at = 0
ticker = False      # line 2 is a ticker
scroll_tick = 0
scroll_pos = [0, 0]
loops_done = 0      # ticker cycles since the screen was drawn

def window(row, pos):
    text = display[row]
    if row == 1 and ticker:
        text += TICKER_SEP
        text *= COLUMNS // len(text) + 2
    return text[pos:pos + COLUMNS]

def show(line1, line2='', ticker_line=False, later=None):
    """Draw a screen; `later` replaces line 1 after TITLE_TIME. Redrawing
    the same ticker screen with new ticker text (the uptime changed, say)
    keeps its place; an identical screen is left alone, so a refresh doesn't
    restart a scrolling line."""
    global scroll_tick, ticker, loops_done, title, title_later, drawn_at
    with ui_lock:
        if not display_active():
            redraw()    # drawn afresh when it wakes
            return
        same = title == line1 and title_later == later
        if same and ticker_line and ticker:
            if display[1] != line2:
                display[1] = line2
                scroll_pos[1] = -1      # redrawn at the next step
            return
        if same and display[1] == line2 and ticker_line == ticker:
            return
        display[:] = [line1, line2]
        title, title_later, drawn_at = line1, later, time.time()
        ticker = ticker_line
        scroll_tick = loops_done = 0
        scroll_pos[:] = [0, 0]
        lcd.clear()
        lcd.write(1, line1)            # write() shows the first 16 columns
        lcd.write(2, window(1, 0) if ticker else line2)

def redraw():
    """Make the next show() draw even if nothing changed (on wake-up)."""
    global title
    display[:] = [None, None]
    title = None

def scroll_loop():
    global scroll_tick, loops_done
    pause = round(SCROLL_PAUSE / SCROLL_STEP)
    while True:
        time.sleep(SCROLL_STEP)
        with ui_lock:
            if not backlight_on or None in display:
                continue
            if title_later and display[0] != title_later and \
                    time.time() - drawn_at >= TITLE_TIME:
                display[0] = title_later
                scroll_pos[0] = 0
                lcd.write(1, title_later[:COLUMNS].ljust(COLUMNS), echo=False)
            loops = [row == 1 and ticker for row in range(2)]
            # a ticker goes all the way round; other lines bounce, holding
            # the end as well as the start
            spans = [len(text) + len(TICKER_SEP) if loops[row]
                     else max(0, len(text) - COLUMNS)
                     for row, text in enumerate(display)]
            if not any(spans):
                continue
            # one cycle for both rows, as long as the longer one needs, so a
            # line only just too long moves once and waits rather than flicking
            cycle = pause + max(span + (0 if loops[row] else pause) if span else 0
                                for row, span in enumerate(spans))
            scroll_tick += 1
            tick = scroll_tick % cycle
            for row in range(2):
                if not spans[row]:
                    continue
                if tick < pause:
                    pos = 0
                elif loops[row]:
                    pos = (tick - pause) % spans[row]
                else:
                    pos = min(tick - pause, spans[row])
                if pos != scroll_pos[row]:
                    scroll_pos[row] = pos
                    lcd.write(row + 1, window(row, pos), echo=False)
            if tick == 0:
                loops_done += 1
                alert_looped()

def show_version():
    sys_name = platform.node().split('.')[0]
    sys_vers = f'{platform.system()} ({platform.machine()})'
    show(sys_name, sys_vers)

def show_truenas():
    # TrueNAS CORE exposes the middleware `cli`. Plain FreeBSD does not -
    # fall back to freebsd-version so the script still works there.
    if shutil.which('cli'):
        truenas = shell('cli -c \'system version\'').split('-')
        line1, line2 = '-'.join(truenas[:-1]), truenas[-1]
    else:
        vers = shell('freebsd-version -u').split('-', 1)
        line1 = f'FreeBSD {vers[0]}'
        line2 = vers[1] if len(vers) > 1 else ''

    show(line1, line2)

def uptime_text(short=False):
    # kern.boottime: "{ sec = 1764075000, usec = 0 } Wed Nov 25 ..."
    boot = int(re.search(r'sec = (\d+)', shell('sysctl -n kern.boottime')).group(1))
    mins = int(time.time() - boot) // 60
    days, hours, mins = mins // 1440, mins // 60 % 24, mins % 60
    if short and days:
        return f'{days}d {hours}h'
    return f'{days}d {hours}h {mins:02d}m' if days else f'{hours}h {mins:02d}m'

def show_uptime():
    load = os.getloadavg()
    show(f'Up {uptime_text()}', f'Load {load[0]:.2f} {load[1]:.2f}')

#
# Network interfaces
#
ip_addresses = []
def find_ips():
    # FreeBSD has no `ip -json` equivalent, so parse `ifconfig -a` text output.
    # Interface blocks start at column 0 ("em0: flags=..."); everything indented
    # underneath belongs to that interface.
    output = shell('ifconfig -a')

    interfaces = {}
    order = []
    current = None

    iface_re = re.compile(r'^(\S+?):')
    laggport_re = re.compile(r'^laggport:\s+(\S+)')

    for line in output.split('\n'):
        if line and not line[0].isspace():
            match = iface_re.match(line)
            if not match:
                current = None
                continue
            name = match.group(1)
            current = {'addr': None, 'status': None, 'lagg': None}
            interfaces[name] = current
            order.append(name)
        elif current is not None:
            stripped = line.strip()

            if stripped.startswith('inet ') and current['addr'] is None:
                # inet 192.168.1.10 netmask 0xffffff00 broadcast 192.168.1.255
                parts = stripped.split()
                if len(parts) > 1:
                    current['addr'] = parts[1]

            elif stripped.startswith('status:'):
                current['status'] = stripped.split(':', 1)[1].strip()

            member_match = laggport_re.match(stripped)
            if member_match:
                member = member_match.group(1)
                if member in interfaces:
                    interfaces[member]['lagg'] = name

    found = []
    links = {}      # interface -> {'status', 'lagg'}, for those with a link status
    for name in order:
        if name == 'lo0':
            continue

        iface = interfaces[name]
        if iface['status']:
            links[name] = {'status': iface['status'], 'lagg': iface['lagg']}

        # lagg (FreeBSD bonding) members carry no IP of their own -
        # show link status instead, matching a Linux bond slave.
        if iface['lagg']:
            found.append((name, iface['status'] or 'down'))
        else:
            found.append((name, iface['addr'] or '0.0.0.0'))
    return found, links

#
# Link watch: a port that has had a link since the service started and
# loses it is a warning. With lagg, a member port down while the lagg is up
# says so; with the whole lagg down, one warning for the lagg (not one per
# port). devd(8) reports LINK_UP / LINK_DOWN as they happen (devd_loop), so
# this doesn't wait for the next refresh.
#
link_seen = set()   # interfaces seen with "status: active"

def link_notices(links):
    for name in list(link_seen):
        if name not in links:
            link_seen.discard(name)     # interface removed: on purpose
    link_seen.update(n for n, l in links.items() if l['status'] == 'active')
    down = {n for n in link_seen if links[n]['status'] != 'active'}
    found = []
    for name in sorted(down):
        lagg = links[name]['lagg']
        if lagg in down:
            continue                    # covered by the lagg's own warning
        if lagg:
            detail = f'port of {lagg}, {lagg} still up'
        elif any(l['lagg'] == name for l in links.values()):
            detail = 'all ports down'
        else:
            detail = links[name]['status']    # "no carrier"
        found.append(('error', f'{name} link down', detail, None))
    return found

DEVD_PIPE = '/var/run/devd.pipe'

def devd_loop():
    """Refresh at once when devd reports a link change:
         !system=IFNET subsystem=em0 type=LINK_DOWN"""
    import socket
    last_error = None
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(DEVD_PIPE)
                last_error = None
                for line in sock.makefile('r', errors='replace'):
                    if 'system=IFNET' in line and re.search(r'type=LINK_(UP|DOWN)', line):
                        refresh_now.set()
        except OSError as e:
            if str(e) != last_error:    # once, not every minute
                print(f'devd: {e}; link changes wait for the next refresh', file=sys.stderr)
            last_error = str(e)
        time.sleep(60)  # devd restarting, or not there (not FreeBSD)

def show_ip(index):
    name, addr = ip_addresses[index]
    show(name, addr)

lan_links = []      # interfaces with link, lagg members left out (the lagg counts their traffic)

#
# ZFS pools
#
zfs_pools = []
def find_zpools():
    pools = shell('zpool list').split('\n')
    return [pool.split() for pool in pools[1:]]

# zpool list: NAME SIZE ALLOC FREE CKPOINT EXPANDSZ FRAG CAP DEDUP HEALTH ALTROOT
def show_zpool(index):
    pool = zfs_pools[index]
    show(f'{pool[0]} {pool[9]}', f'{pool[2]} of {pool[1]} ({pool[7]})')

#
# Notices: things worth a look, each ('error' | 'maint', line1, line2, disk);
# disk is the adaN a notice is about (for the web panel's bays), else None.
# Errors turn the status LED red and pop up on the display; maintenance
# (scrub, resilver) makes it alternate red/green.
#
notices = []
zfs_labels = {}     # disk -> (pool, name in zpool status), for the web panel
smart_notices = []

DISK_RE = re.compile(r'(ada|da)\d+')   # whole-disk device names

def disk_names():
    """diskid/DISK-XXXX (and other labels) -> adaN, from `glabel status`."""
    names = {}
    try:
        for line in shell('glabel status').split('\n')[1:]:
            parts = line.split()
            if len(parts) == 3:
                names[parts[0]] = re.sub(r'p\d+$', '', parts[2])
    except (subprocess.CalledProcessError, OSError):
        pass
    return names

pool_scans = {}     # pool -> {'running', 'progress', 'last', 'canceled'}

def pool_notices():
    """-> (notices, {disk: (pool, name ZFS knows it by)}, {pool: scan}),
    from `zpool status`."""
    found = []
    labels = {}
    scans = {}
    output = shell('zpool status')
    names = disk_names()

    for block in re.split(r'^(?=\s*pool: )', output, flags=re.M):
        match = re.search(r'^\s*pool: (\S+)', block, re.M)
        if not match:
            continue
        pool = match.group(1)

        state = re.search(r'^\s*state: (\S+)', block, re.M)
        if state and state.group(1) != 'ONLINE':
            found.append(('error', f'{pool} {state.group(1)}', 'see zpool status', None))

        scan = re.search(r'(scrub|resilver) in progress', block)
        # finished: "scan: scrub repaired 0B in 05:12:33 with 0 errors on Sun Nov 22 03:12:33 2026"
        # stopped:  "scan: scrub canceled on Wed Nov 25 16:40:01 2026"
        last = re.search(r'scrub (repaired .*|canceled) on \w+ (\w+)\s+(\d+) ', block)
        scans[pool] = {'running': scan.group(1) if scan else None, 'progress': '',
                       'last': f'{last.group(2)} {last.group(3)}' if last else None,
                       'canceled': bool(last) and last.group(1) == 'canceled'}
        if scan:
            # "0B repaired, 45.12% done, 10:30:52 to go" (or "1 days 02:03:04 to go")
            done = re.search(r'([\d.]+)% done', block)
            eta = re.search(r'(?:(\d+) days? )?(\d+):(\d+):\d+ to go', block)
            parts = [f'{float(done.group(1)):.0f}%'] if done else []
            if eta:
                days, hours, mins = (int(g or 0) for g in eta.groups())
                parts.append((f'{days}d {hours}h' if days else
                              f'{hours}h {mins:02d}m' if hours else f'{mins}m') + ' left')
            scans[pool]['progress'] = ', '.join(parts)
            what = 'scrubbing' if scan.group(1) == 'scrub' else 'resilvering'
            found.append(('maint', f'{pool} {what}', scans[pool]['progress'] or 'starting', None))

        # vdev rows under "config:": NAME STATE READ WRITE CKSUM
        config = block.split('config:', 1)[-1].split('errors:', 1)[0]
        for line in config.split('\n'):
            cols = line.split()
            if len(cols) < 5 or cols[0] in ('NAME', pool):
                continue
            # ada0, diskid/DISK-XXXX, gpt/label: which adaN it is, for the
            # web panel's bay labels (raidz/mirror rows aren't disks)
            disk = names.get(cols[0], cols[0].split('/')[-1])
            if DISK_RE.fullmatch(disk):
                labels[disk] = (pool, cols[0])
            read, write, cksum = cols[2:5]
            if not all(re.fullmatch(r'[\d.]+[KMGT]?', c) for c in (read, write, cksum)):
                continue
            if (read, write, cksum) != ('0', '0', '0'):
                found.append(('error', f'{disk} errors',
                              f'read {read} write {write} checksum {cksum}', disk))

        errors = re.search(r'^errors: (\d+) data errors', block, re.M)
        if errors:
            found.append(('error', f'{pool} data errors',
                          f'{errors.group(1)} - see zpool status -v', None))
    return found, labels, scans

def check_smart():
    """One pass over every disk with smartctl; results go to smart_notices."""
    smartctl = shutil.which('smartctl')
    if not smartctl:
        return
    found = []
    disks = shell('sysctl -n kern.disks').split()
    for disk in disks:
        # an hourly full attribute table per disk would swamp the log
        rc, out = run(f'{smartctl} -H -A /dev/{disk}', timeout=60, log=False)
        # "SMART overall-health self-assessment test result: PASSED"
        # (SAS disks say "SMART Health Status: OK"); disks without SMART
        # (USB sticks) have neither line and are skipped
        health = re.search(r'self-assessment test result: (\S+)|'
                           r'SMART Health Status: (\S+)', out)
        if health and (health.group(1) or health.group(2)) not in ('PASSED', 'OK'):
            found.append(('error', f'{disk} SMART', 'health check FAILED', disk))
            continue
        # attribute table: ID NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW
        raw = {}
        for line in out.split('\n'):
            cols = line.split()
            if len(cols) >= 10 and cols[0].isdigit() and cols[9].isdigit():
                raw[cols[0]] = int(cols[9])
        bad = [f'{raw[attr]} {label}'
               for attr, label in (('5', 'reallocated'), ('197', 'pending'),
                                   ('198', 'uncorrectable'))
               if raw.get(attr, 0) > 0]
        if bad:
            found.append(('error', f'{disk} SMART',
                          'sectors: ' + ', '.join(bad), disk))
    print(f'SMART: {len(disks)} disks checked, '
          + ('; '.join(f'{n[1]}: {n[2]}' for n in found) or 'no problems'), flush=True)
    with ui_lock:
        smart_notices[:] = found

def smart_loop():
    while True:
        try:
            check_smart()
        except (subprocess.SubprocessError, OSError) as e:
            print(f'SMART check failed: {e}', file=sys.stderr)
        time.sleep(SMART_INTERVAL)

def mark(level):
    return '!' if level == 'error' else '*'

def show_notice(index):
    level, line1, line2, _ = notices[index]
    show(f'{mark(level)}{line1}', line2)

def plural(n, word):
    return f'{n} {word}' + ('s' if n != 1 else '')

def notice_count():
    """'!2 *1': warnings (errors) and other notices; '' with none."""
    errors = sum(1 for n in notices if n[0] == 'error')
    others = len(notices) - errors
    return ' '.join(([f'!{errors}'] if errors else []) + ([f'*{others}'] if others else []))

def status_line2():
    errors = sum(1 for n in notices if n[0] == 'error')
    if errors:
        return plural(errors, 'warning')
    return plural(len(notices), 'notice') if notices else 'notices, pools'

status_led = None   # what the status LED should show, driven or not

def status_led_driven():
    return not args.no_status_led and os.access(QNAPLED, os.X_OK)

def set_status_led(colour):
    global status_led
    if colour == status_led:
        return
    status_led = colour
    web_notify()
    if not status_led_driven():
        return
    subprocess.run([QNAPLED, 'status-led', colour], stdout=subprocess.DEVNULL)

#
# Updates: packages (pkg) and the base system (freebsd-update)
#
updates = {'pkg': None, 'os': None, 'busy': None}
update_wakeup = threading.Event()

def check_updates():
    with ui_lock:
        updates['busy'] = 'checking (takes a few minutes)'
    try:
        run('pkg update -q', timeout=600)
        _, out = run("pkg version -q -v -R -l '<'", timeout=600)
        pkg = len([l for l in out.split('\n') if l.strip()])
        run('freebsd-update --not-running-from-cron fetch', timeout=1800)
        rc, _ = run('freebsd-update updatesready', timeout=60)
        with ui_lock:
            updates['pkg'], updates['os'] = pkg, rc == 0
    finally:
        with ui_lock:
            updates['busy'] = None
            redraw_if('updates')

def set_busy(text):
    with ui_lock:
        updates['busy'] = text
        redraw_if('updates')

def update_loop():
    time.sleep(60)  # let the system settle after boot
    while True:
        try:
            check_updates()
        except (subprocess.SubprocessError, OSError) as e:
            print(f'update check failed: {e}', file=sys.stderr)
        update_wakeup.wait(UPDATE_INTERVAL)
        update_wakeup.clear()

def updates_pending():
    return bool(updates['pkg']) or bool(updates['os'])

def reboot_needed():
    # the installed kernel differs from the running one until a reboot
    try:
        return shell('freebsd-version -k') != shell('freebsd-version -r')
    except (subprocess.CalledProcessError, OSError):
        return False

def updates_text():
    """'Updates: 3 packages + FreeBSD', or '' with nothing waiting."""
    if not updates_pending():
        return ''
    n = updates['pkg']
    parts = ([plural(n, 'package')] if n else []) + (['FreeBSD'] if updates['os'] else [])
    return 'Updates: ' + ' + '.join(parts)

def show_updates():
    # freebsd-update fetch takes minutes on an Atom: while a check runs, keep
    # showing the last result on line 1
    if reboot_needed() and not updates['busy']:
        show('Reboot needed', 'Enter: reboot now')
        return
    if updates['pkg'] is None:
        line1 = 'Updates'
    elif not updates_pending():
        line1 = 'Up to date'
    else:
        line1 = updates_text()

    if updates['busy']:
        line2 = updates['busy']
    elif updates['pkg'] is None:
        line2 = 'not checked yet'
    elif updates_pending():
        line2 = 'Enter: install'
    else:
        line2 = 'Enter: check again now'
    show(line1, line2)

def select_updates():
    if updates['busy']:
        return
    if reboot_needed():
        prompt('Reboot now?', reboot)
        return
    if not updates_pending():
        # nothing to install: check again now
        update_wakeup.set()
        return
    prompt('Install updates?', install_updates)

def install_updates():
    def work():
        try:
            if updates['pkg']:
                set_busy('installing packages...')
                rc, _ = run('pkg upgrade -y', timeout=3600)
                if rc != 0:
                    raise RuntimeError('pkg upgrade failed')
            if updates['os']:
                set_busy('installing FreeBSD update...')
                rc, _ = run('freebsd-update --not-running-from-cron install', timeout=3600)
                if rc != 0:
                    raise RuntimeError('freebsd-update failed')
            with ui_lock:
                updates['pkg'], updates['os'], updates['busy'] = 0, False, None
            # a new kernel (or base libraries) only takes effect after a reboot
            if reboot_needed():
                # if this goes unanswered, the Updates screen keeps offering it
                prompt('Reboot now?', reboot)
            else:
                with ui_lock:
                    show('Updates', 'installed')
        except (RuntimeError, subprocess.SubprocessError, OSError) as e:
            print(f'update failed: {e}', file=sys.stderr)
            with ui_lock:
                updates['busy'] = None
                show('Update failed', 'see /var/log/qnaplcd.log')
        finally:
            update_wakeup.set()  # re-check what is left

    threading.Thread(target=work, daemon=True).start()

def reboot():
    show('Rebooting...', '')
    run('shutdown -r now')

def power_off():
    show('Shutting down...', '')
    run('shutdown -p now')

#
# Scrubs, from the Maintenance menu
#
def show_scrub(pool):
    scan = pool_scans.get(pool, {})
    if scan.get('running') == 'scrub':
        show(f'{pool} scrubbing', ', '.join(filter(None, [scan['progress'], 'Enter: stop'])))
    elif scan.get('running'):
        show(f'{pool} resilvering', scan['progress'])
    else:
        if not scan.get('last'):
            last = 'never scrubbed'
        else:
            last = f'{"canceled" if scan["canceled"] else "last"} {scan["last"]}'
        show(f'Scrub {pool}', f'{last}, Enter: start')

def select_scrub(pool):
    running = pool_scans.get(pool, {}).get('running')
    if running == 'scrub':
        prompt('Stop scrub?', partial(scrub, pool, stop=True))
    elif not running:
        prompt(f'Scrub {pool}?', partial(scrub, pool))

def scrub(pool, stop=False):
    def work():
        run(f'zpool scrub {"-s " if stop else ""}{shlex.quote(pool)}', timeout=60)
        refresh_now.set()   # show the new state (and the notice) promptly
    threading.Thread(target=work, daemon=True).start()

#
# Overview: the screen the display wakes up to. Line 1 says "1. Overview" for
# TITLE_TIME, then the host, with the count of warnings (!) and notices
# (*); line 2 a short ticker: notices, uptime, pool use,
# updates. The details are in the menus.
#
def status_ticker():
    # warnings by name only (Status has the details); scrubs with progress
    items = [f'{mark(level)}{l1}' + (f' {l2}' if level == 'maint' else '')
             for level, l1, l2, _ in notices]
    items.append(f'Up {uptime_text(short=True)}')
    items += [f'{p[0]} {p[7]} used' for p in zfs_pools]
    if not args.no_updates:
        if reboot_needed():
            items.append('Reboot needed')
        elif updates_pending():
            items.append(plural((updates['pkg'] or 0) + bool(updates['os']), 'update'))
    return TICKER_SEP.join(items)

def show_overview():
    count = notice_count()
    def line1(name):
        return name + ' ' * max(1, COLUMNS - len(name) - len(count)) + count if count else name
    show(line1('1. Overview'), status_ticker(), ticker_line=True, later=line1(HOST))

#
# Alerts: a new notice wakes the panel and scrolls by ALERT_LOOPS times
# before it sleeps again; a new warning (error) stays lit until a button
# is pressed. Either way the next screen is the overview.
#
alert = None        # None, 'maint' or 'error'
alerted = set()     # (level, line 1) of notices already alerted

def show_alert():
    shown = [n for n in notices if n[0] == alert] or notices
    word = 'warning' if alert == 'error' else 'notice'
    title = f'{mark(alert)} {word.capitalize()}' if len(shown) == 1 \
        else f'{mark(alert)} {plural(len(shown), word)}'
    show(title, TICKER_SEP.join(f'{l1}: {l2}' for _, l1, l2, _ in shown),
         ticker_line=True)

def check_alerts():
    """After a refresh: start, change or end the alert."""
    global alert
    keys = {(n[0], n[1]) for n in notices}
    alerted.intersection_update(keys)   # a notice that comes back alerts again
    new = keys - alerted
    if new and prompt_state is None:
        alerted.update(new)
        errors = alert == 'error' or any(level == 'error' for level, _ in new)
        alert = 'error' if errors else 'maint'
        redraw()
        lcd_on()
    elif alert == 'error' and not any(n[0] == 'error' for n in notices):
        # the warning went away by itself
        end_alert(sleep=False)
    elif alert and not notices:
        end_alert(sleep=False)

def alert_looped():
    # from scroll_loop, each time the ticker has gone all the way round
    if alert == 'maint' and loops_done >= ALERT_LOOPS:
        end_alert(sleep=True)

def end_alert(sleep):
    global alert
    alert = None
    go_home()
    if sleep:
        lcd_off()
    else:
        draw_current()
        lcd_on()

#
# Menus. The main menu is the overview and one numbered entry per submenu;
# Select steps through a menu (round to its first screen again), Enter
# opens an entry or runs a screen's action. Each submenu ends with Back.
# Entries Enter does something with show ' >' beside it, like the prompts.
#
Item = namedtuple('Item', 'key draw action')    # action None: Enter does nothing

menus = {}
stack = [['Main', 0]]   # [menu name, index] from the main menu down

def show_entry(label, line2):
    show(f'{label:<{COLUMNS - 2}} >', line2)

def entry(key, label, line2, action):
    # line2 may be a function, for text that changes
    return Item(key, lambda: show_entry(label, line2() if callable(line2) else line2), action)

def build_menus():
    status = [Item(f'notice:{level}:{l1}', partial(show_notice, i), None)
              for i, (level, l1, _, _) in enumerate(notices)]
    if not notices:
        status.append(Item('notice:none', lambda: show('Notices', 'none'), None))
    status += [Item(f'pool:{p[0]}', partial(show_zpool, i), None)
               for i, p in enumerate(zfs_pools)]

    info = [Item('version', show_version, None),
            Item('os', show_truenas, None),
            Item('uptime', show_uptime, None)]
    info += [Item(f'ip:{name}', partial(show_ip, i), None)
             for i, (name, _) in enumerate(ip_addresses)]

    maint = []
    if not args.no_updates:
        maint.append(Item('updates', show_updates, select_updates))
    maint += [Item(f'scrub:{p[0]}', partial(show_scrub, p[0]), partial(select_scrub, p[0]))
              for p in zfs_pools]
    maint += [entry('reboot', 'Reboot', 'restart the NAS', partial(prompt, 'Reboot now?', reboot)),
              entry('poweroff', 'Shut down', 'power off the NAS',
                    partial(prompt, 'Power off now?', power_off))]

    def maint_line2():
        return updates_text() or 'updates, scrub, power'

    built = {
        'Main': [Item('overview', show_overview, next_screen),
                 entry('open:Status', '2. Status', status_line2, partial(open_menu, 'Status')),
                 entry('open:Info', '3. Info', 'host, uptime, IPs', partial(open_menu, 'Info')),
                 entry('open:Maintenance', '4. Maintenance', maint_line2,
                       partial(open_menu, 'Maintenance'))],
        'Status': status,
        'Info': info,
        'Maintenance': maint,
    }
    for name, items in built.items():
        if name != 'Main':
            items.append(entry('back', 'Back', 'to main menu', go_back))
    return built

def rebuild_menus():
    """Rebuild every menu from the latest data, staying on the same screen
    (by key) where it still exists."""
    keys = [menus[name][i].key if name in menus and i < len(menus[name]) else None
            for name, i in stack]
    menus.clear()
    menus.update(build_menus())
    for level, (name, i) in enumerate(stack):
        found = [item.key for item in menus[name]]
        stack[level][1] = found.index(keys[level]) if keys[level] in found \
            else min(i, len(found) - 1)

def current_item():
    name, i = stack[-1]
    return menus[name][i]

def at_overview():
    return alert is None and stack == [['Main', 0]]

def go_home():
    stack[:] = [['Main', 0]]

def draw_current():
    if not display_active():
        redraw()        # nobody watching: skip the work, draw on wake-up
        return
    if alert:
        show_alert()
    else:
        current_item().draw()

def next_screen():
    name, i = stack[-1]
    stack[-1][1] = (i + 1) % len(menus[name])
    draw_current()

def open_menu(name):
    stack.append([name, 0])
    draw_current()

def go_back():
    if len(stack) > 1:
        stack.pop()
    draw_current()

def redraw_if(key):
    """Redraw if the screen `key` is up (no prompt or alert over it)."""
    if prompt_state is None and alert is None and current_item().key == key:
        current_item().draw()

def screen_label():
    """Where the menu is, for the web view: 'Maintenance 2 of 5'."""
    if alert:
        return 'alert: any button dismisses'
    if at_overview():
        return 'overview: Enter for the menu'
    name, i = stack[-1]
    return f'{name} {i + 1} of {len(menus[name])}'

refresh_now = threading.Event()

def refresh():
    global zfs_labels
    ips, links = find_ips()
    active = [n for n, l in links.items() if l['status'] == 'active' and not l['lagg']]
    pools = find_zpools()
    found, labels, scans = pool_notices()
    with ui_lock:
        ip_addresses[:] = ips
        lan_links[:] = active
        zfs_pools[:] = pools
        notices[:] = found + link_notices(links) + smart_notices
        zfs_labels = labels     # replaced whole: web threads read it unlocked
        pool_scans.clear()
        pool_scans.update(scans)
        rebuild_menus()
        web_notify()

        if any(n[0] == 'error' for n in notices):
            set_status_led('red')
        elif notices:
            set_status_led('alternate')
        else:
            set_status_led('green')

        check_alerts()
        if prompt_state is None:
            draw_current()

#
# Prompts: a yes/no question. Enter = yes, Select = no.
#
prompt_state = None
prompt_timer = None

# On the TS-x69 the buttons sit to the right of the display, Enter beside
# the top line and Select beside the bottom one, so a question is laid out
# with its answers next to them:
#   |Install    Yes >|   <- Enter
#   |updates?    No >|   <- Select
YES_LABEL, NO_LABEL = ' Yes >', ' No >'

def prompt_lines(question):
    """Word-wrap the question into the space left of the labels."""
    widths = (COLUMNS - len(YES_LABEL), COLUMNS - len(NO_LABEL))
    rows, words = ['', ''], question.split()
    row = 0
    for word in words:
        joined = f'{rows[row]} {word}'.strip()
        if len(joined) <= widths[row]:
            rows[row] = joined
        elif row == 0 and len(word) <= widths[1]:
            row, rows[1] = 1, word
        else:
            # too long to label: question on top, answers spelled out below
            return question, 'Enter=yes Sel=no'
    return (rows[0].ljust(widths[0]) + YES_LABEL,
            rows[1].ljust(widths[1]) + NO_LABEL)

def prompt(question, on_yes):
    global prompt_state, prompt_timer
    with ui_lock:
        prompt_state = on_yes
        lcd_on()
        show(*prompt_lines(question))
        if prompt_timer:
            prompt_timer.cancel()
        prompt_timer = threading.Timer(PROMPT_TIMEOUT, lambda: answer(False))
        prompt_timer.daemon = True
        prompt_timer.start()

def answer(yes):
    global prompt_state
    with ui_lock:
        on_yes, prompt_state = prompt_state, None
        if prompt_timer:
            prompt_timer.cancel()
        if on_yes is None:
            return
        if yes:
            show('OK', '')
            on_yes()
        else:
            draw_current()

#
# Buttons, labelled Enter and Select on the TS-x69. The panel reports the
# buttons held after every change as a bitmask, and 0x00 once all are
# released (pressing both reads e.g. 0x02 0x03 0x01 0x00). Act on release:
# Select moves to the next screen, Enter runs the screen's action (or says
# yes to a prompt). Both together does nothing. A press while the display
# is dark only wakes it (to the overview), and a press during an alert
# only dismisses it.
#
ENTER = 0x01
SELECT = 0x02
held = 0
waking = False      # this press lit the display: don't act on it
buttons_down = 0    # as last reported by the panel, for the web view

def select_pressed():
    if prompt_state is not None:
        answer(False)
    else:
        next_screen()

# What a press from the web panel may do: move around the menus. Anything
# else (saying yes to a prompt, or an action that runs a command: updates,
# scrubs, reboot) needs the real buttons. This is an allowlist checked here,
# where the action runs, not in the HTTP handler, so no request can get
# round it; a new menu action is refused from the web until it is added.
def web_allowed(action):
    func = action.func if isinstance(action, partial) else action
    return func in (next_screen, open_menu, go_back)

def enter_pressed(remote=False):
    """remote: the press came from the web panel. Returns False if it was
    refused."""
    if prompt_state is not None:
        if remote:
            return False
        answer(True)
        return True
    action = current_item().action
    if action:
        if remote and not web_allowed(action):
            return False
        action()
    return True

def response_handler(command, data):
    # runs on the serial reader thread: an exception here would kill it and
    # leave the buttons dead until the service restarts
    try:
        handle_buttons(command, data)
    except Exception as e:
        print(f'button handling failed: {e!r}', file=sys.stderr)

def handle_buttons(command, data, remote=False):
    """remote: the event came from the web panel (web_press). Returns False
    if a remote press was refused (see web_allowed)."""
    global held, buttons_down, waking

    if command != 'Switch_Status':
        return True

    with ui_lock:
        if buttons_down != data & 0x03:
            buttons_down = data & 0x03
            web_notify()
        if data & 0x03:
            if not held and not backlight_on:
                waking = True
                lcd_on()
                redraw()            # start the status ticker from the top
                draw_current()
            held |= data & 0x03
            lcd_on()
            return True

        pressed, held = held, 0
        allowed = True
        if waking:
            waking = False
        elif prompt_state is None and alert:
            end_alert(sleep=False)
        elif pressed == ENTER:
            allowed = enter_pressed(remote)
        elif pressed == SELECT:
            select_pressed()
        lcd_on()    # the timeout depends on the screen now showing
        return allowed

#
# Web panel (--web): a live copy of the display, LEDs and buttons in a
# browser. The display comes from what QnapLCD last sent to the panel.
#
web = None
HOST = platform.node().split('.')[0]

def web_notify():
    if web:
        web.notify()

# Drive bays, left to right, by the AHCI channel behind each. On the TS-x69
# the six bays hang off three Marvell 88SE9125s, two ports each, which
# FreeBSD numbers ahcich0-5; the onboard Intel ports (ahcich6+) are internal.
# Checked on a TS-669: ada0-ada5 (ahcich0-5) light bays 1-6, left to right.
BAY_CHANNELS = ['ahcich0', 'ahcich1', 'ahcich2', 'ahcich3', 'ahcich4', 'ahcich5']
BAY_RESCAN = 30         # seconds between looks for added/removed disks
bay_disks = [None] * len(BAY_CHANNELS)   # adaN in each bay, None if empty
disk_busy = set()       # disks with I/O in the last second

def find_bay_disks():
    """Bay -> disk from `camcontrol devlist -v` (needs root):
         scbus0 on ahcich0 bus 0:
         <ST2000DL003-9VT166 CC32>  at scbus0 target 0 lun 0 (pass0,ada0)"""
    by_channel, channel = {}, None
    for line in shell('camcontrol devlist -v').split('\n'):
        match = re.match(r'scbus\d+ on (\S+) bus', line)
        if match:
            channel = match.group(1)
            continue
        match = re.search(r'\(([^)]*)\)\s*$', line)
        if match and channel:
            disks = [d for d in match.group(1).split(',') if DISK_RE.fullmatch(d)]
            if disks:
                by_channel[channel] = disks[0]
    return [by_channel.get(ch) for ch in BAY_CHANNELS]

LAN_BUSY = 4096         # bytes/s before the LAN light flickers; the page's
                        # own updates are well under this
lan_busy = False

def interface_bytes(netstat):
    """`netstat -ibn -f link` -> {interface: in + out bytes}.
         Name Mtu Network Address Ipkts Ierrs Idrop Ibytes Opkts Oerrs Obytes Coll"""
    lines = netstat.split('\n')
    head = lines[0].split() if lines else []
    if 'Ibytes' not in head or 'Obytes' not in head:
        return {}
    ib, ob = head.index('Ibytes'), head.index('Obytes')
    counts = {}
    for line in lines[1:]:
        cols = line.split()
        if len(cols) == len(head) and cols[ib].isdigit() and cols[ob].isdigit():
            counts[cols[0]] = int(cols[ib]) + int(cols[ob])
    return counts

def activity_loop():
    """While someone has the web panel open: which disk is in each bay
    (every BAY_RESCAN s), and once a second which disks and whether the LAN
    are busy. One shell per second runs `netstat` and `iostat` together.
    Nothing runs while no page is open."""
    global lan_busy
    last_scan = 0
    last_bytes, last_time = None, None
    while True:
        if not web.clients:
            # idle: forget what was seen, so the next page starts fresh
            last_scan, last_bytes = 0, None
            if disk_busy or lan_busy:
                disk_busy.clear()
                lan_busy = False
                web_notify()
            time.sleep(1)
            continue

        if time.time() - last_scan > BAY_RESCAN:
            last_scan = time.time()
            try:
                found = find_bay_disks()
            except (subprocess.CalledProcessError, OSError) as e:
                print(f'bay scan failed: {e}', file=sys.stderr)
                found = bay_disks
            if found != bay_disks:
                bay_disks[:] = found
                web_notify()

        disks = [d for d in bay_disks if d]
        # two iostat reports: since boot, then the last second; the last wins.
        # Without disks, sleep 1 keeps the pace.
        disk_cmd = f'iostat -x -d -c 2 -w 1 {" ".join(disks)}' if disks else 'sleep 1'
        try:
            out = shell(f'netstat -ibn -f link; echo ---; {disk_cmd}')
        except (subprocess.CalledProcessError, OSError) as e:
            print(f'activity check failed: {e}', file=sys.stderr)
            time.sleep(BAY_RESCAN)
            continue
        now_time = time.time()
        net_out, _, disk_out = out.partition('\n---\n')

        rate = {}
        for line in disk_out.split('\n'):
            cols = line.split()
            # device r/s w/s kr/s kw/s ... (the second report overwrites the first)
            if len(cols) >= 5 and cols[0] in disks:
                try:
                    rate[cols[0]] = float(cols[3]) + float(cols[4])
                except ValueError:
                    pass
        busy = {d for d, kb in rate.items() if kb > 0}

        counts = interface_bytes(net_out)
        total = sum(counts.get(name, 0) for name in lan_links)
        net_busy = False
        if last_bytes is not None and now_time > last_time:
            net_busy = (total - last_bytes) / (now_time - last_time) > LAN_BUSY
        last_bytes, last_time = total, now_time

        if busy != disk_busy or net_busy != lan_busy:
            disk_busy.clear()
            disk_busy.update(busy)
            lan_busy = net_busy
            web_notify()

def bay_state():
    errors = {n[3] for n in notices if n[0] == 'error'}
    return [{'disk': disk,
             'state': 'empty' if not disk else 'error' if disk in errors else 'ok',
             'busy': disk in disk_busy,
             'zfs': zfs_labels.get(disk)}   # [pool, name] or None
            for disk in bay_disks]

def web_state():
    # no ui_lock: this runs on the web server's threads and only reads
    with lcd.state_lock:
        lines = list(lcd.shown)
    return {
        'host': HOST,
        'lines': lines,
        'backlight': lcd.lit,
        'status_led': status_led,
        'status_led_driven': status_led_driven(),
        'buttons': {'enter': bool(buttons_down & ENTER),
                    'select': bool(buttons_down & SELECT)},
        'prompt': prompt_state is not None,
        'screen': screen_label(),
        'connected': lcd.connection is not None,
        'bays': bay_state(),
        'lan': 'busy' if lan_busy else 'on' if lan_links else 'off',
    }

def web_press(name):
    # the same path as the real buttons: press, then release, marked remote
    # so that nothing but menu navigation runs (web_allowed). Under ui_lock
    # the release can't be merged with a real press from another thread.
    bit = ENTER if name == 'enter' else SELECT
    with ui_lock:
        handle_buttons('Switch_Status', bit, remote=True)
        if not handle_buttons('Switch_Status', 0x00, remote=True):
            raise PermissionError('use the buttons on the NAS for that')

def parse_listen(value):
    """'8080', '127.0.0.1:8080' or '[::]:8080' -> (address, port)."""
    addr, _, port = value.rpartition(':')
    addr = addr.strip('[]') or '0.0.0.0'
    try:
        port = int(port)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not [ADDRESS:]PORT: {value}')
    return addr, port

def handle_sigterm(signum, frame):
    # rc.d stop (via daemon(8)) sends SIGTERM; unwind through main()'s finally
    sys.exit(0)

def main():
    global lcd, args, web

    parser = argparse.ArgumentParser(description='QNAP front-panel LCD menu')
    parser.add_argument('--port', default=PORT, help=f'serial device (default {PORT})')
    parser.add_argument('--speed', type=int, default=PORT_SPEED, help=f'baud rate (default {PORT_SPEED})')
    parser.add_argument('--refresh', type=int, default=30, help='seconds between interface/pool rescans (default 30)')
    parser.add_argument('--no-status-led', action='store_true', help='leave the status LED alone')
    parser.add_argument('--no-updates', action='store_true', help='do not check for or offer updates')
    parser.add_argument('--web', metavar='[ADDRESS:]PORT', type=parse_listen,
                        help='serve a live web view of the panel (no authentication: '
                             'anyone who can reach it can press the buttons)')
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_sigterm)

    # rc(8) runs services with only the base system on PATH; smartctl and
    # the real pkg live under /usr/local
    path = os.environ.get('PATH', '/sbin:/bin:/usr/sbin:/usr/bin').split(':')
    for extra in ('/usr/local/sbin', '/usr/local/bin'):
        if extra not in path:
            path.append(extra)
    os.environ['PATH'] = ':'.join(path)

    if not args.no_updates:
        threading.Thread(target=update_loop, daemon=True).start()
    threading.Thread(target=smart_loop, daemon=True).start()
    threading.Thread(target=devd_loop, daemon=True).start()
    threading.Thread(target=scroll_loop, daemon=True).start()

    with ui_lock:
        rebuild_menus()     # so a button pressed before the first refresh works
    lcd = qnaplcd.QnapLCD(args.port, args.speed, response_handler)
    if args.web:
        from qnaplcd.web import WebPanel
        web = WebPanel(*args.web, web_state, web_press)
        lcd.on_change = web_notify
        web.start()
        threading.Thread(target=activity_loop, daemon=True).start()
    lcd_on()
    lcd.reset()
    lcd.clear()

    lcd.write(0, [platform.node(), 'System Ready...'])

    try:
        while True:
            try:
                refresh()
            except (subprocess.CalledProcessError, OSError, IndexError) as e:
                # a failing shell command must not take the service down
                print(f'refresh failed: {e}', file=sys.stderr)

            refresh_now.wait(args.refresh)
            refresh_now.clear()
    except KeyboardInterrupt:
        pass
    finally:
        if lcd_timer:
            lcd_timer.cancel()
        lcd.backlight(False)

if __name__ == '__main__':
    main()
