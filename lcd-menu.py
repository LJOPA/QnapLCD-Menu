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

DISPLAY_TIMEOUT = 30    # seconds
PROMPT_TIMEOUT = 30     # seconds before an unanswered prompt cancels itself
SMART_INTERVAL = 3600   # seconds between SMART checks
UPDATE_INTERVAL = 6 * 3600  # seconds between update checks
SCROLL_STEP = 0.4       # seconds per character when a line scrolls
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
backlight_on = False

def lcd_off():
    global backlight_on
    backlight_on = False
    lcd.backlight(False)

def lcd_on():
    global lcd_timer, backlight_on

    lcd.backlight(True)
    backlight_on = True

    if lcd_timer:
        lcd_timer.cancel()

    lcd_timer = threading.Timer(DISPLAY_TIMEOUT, lcd_off)
    lcd_timer.daemon = True
    lcd_timer.start()

def shell(cmd):
    return subprocess.check_output(cmd, shell=True, universal_newlines=True).strip()

def run(cmd, timeout=None):
    """Run a command for its side effects; returns (exit code, output)."""
    env = dict(os.environ, PAGER='cat', ASSUME_ALWAYS_YES='yes')
    p = subprocess.run(cmd, shell=True, universal_newlines=True, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    # freebsd-update can list thousands of files; keep the log readable
    lines = p.stdout.rstrip('\n').split('\n')
    if len(lines) > 40:
        lines = lines[:20] + [f'... ({len(lines) - 40} lines skipped) ...'] + lines[-20:]
    print(f'$ {cmd} -> {p.returncode}', *lines, sep='\n', flush=True)
    return p.returncode, p.stdout

COLUMNS = 16

# What is on the display. Lines longer than COLUMNS scroll: scroll_loop()
# holds the start for SCROLL_PAUSE, steps along one character at a time,
# holds the end, then starts over. Only while the backlight is on - at
# 1200 baud each step costs ~0.2 s of serial time.
display = ['', '']
scroll_tick = 0
scroll_pos = [0, 0]

def show(line1, line2=''):
    global scroll_tick
    with ui_lock:
        display[:] = [line1, line2]
        scroll_tick = 0
        scroll_pos[:] = [0, 0]
        lcd.clear()
        lcd.write(0, [line1, line2])   # write() shows the first 16 columns

def scroll_loop():
    global scroll_tick
    pause = round(SCROLL_PAUSE / SCROLL_STEP)
    while True:
        time.sleep(SCROLL_STEP)
        with ui_lock:
            if not backlight_on:
                continue
            spans = [max(0, len(text) - COLUMNS) for text in display]
            if not any(spans):
                continue
            # one cycle for both rows, as long as the longer one needs, so a
            # line only just too long moves once and waits rather than flicking
            scroll_tick += 1
            tick = scroll_tick % (pause + max(spans) + pause)
            for row, text in enumerate(display):
                if not spans[row]:
                    continue
                pos = 0 if tick < pause else min(tick - pause, spans[row])
                if pos != scroll_pos[row]:
                    scroll_pos[row] = pos
                    lcd.write(row + 1, text[pos:pos + COLUMNS], echo=False)

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

def show_uptime():
    # kern.boottime: "{ sec = 1764075000, usec = 0 } Wed Nov 25 ..."
    boot = int(re.search(r'sec = (\d+)', shell('sysctl -n kern.boottime')).group(1))
    mins = int(time.time() - boot) // 60
    days, hours, mins = mins // 1440, mins // 60 % 24, mins % 60
    up = f'{days}d {hours}h {mins:02d}m' if days else f'{hours}h {mins:02d}m'
    load = os.getloadavg()
    show(f'Up {up}', f'Load {load[0]:.2f} {load[1]:.2f}')

def nth(func):
    """Which of the identical `func` entries in the menu is selected."""
    return sum(1 for index in range(menu_item) if menu[index] == func)

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
            current = {'addr': None, 'status': 'down', 'is_member': False}
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
                    interfaces[member]['is_member'] = True

    found = []
    for name in order:
        if name == 'lo0':
            continue

        iface = interfaces[name]

        # lagg (FreeBSD bonding) members carry no IP of their own -
        # show link status instead, matching a Linux bond slave.
        if iface['is_member']:
            found.append((name, iface['status']))
        else:
            found.append((name, iface['addr'] or '0.0.0.0'))

    # ports with a link, for the web panel's LAN light
    active = [name for name in order
              if name != 'lo0' and interfaces[name]['status'] == 'active']
    return found, active

def show_ip():
    name, addr = ip_addresses[nth(show_ip)]
    show(name, addr)

lan_links = []      # interfaces with link (ifconfig "status: active")

#
# ZFS pools
#
zfs_pools = []
def find_zpools():
    pools = shell('zpool list').split('\n')
    return [pool.split() for pool in pools[1:]]

def show_zpool():
    pool = zfs_pools[nth(show_zpool)]
    show(f'{pool[0]} ({pool[7]})', f'{pool[2]} of {pool[1]}')

#
# Notices: things worth a look, each ('error' | 'maint', line1, line2).
# Errors turn the status LED red and pop up on the display; maintenance
# (scrub, resilver) makes it alternate red/green.
#
notices = []
smart_notices = []

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

def pool_notices():
    found = []
    output = shell('zpool status')
    names = disk_names()

    for block in re.split(r'^(?=\s*pool: )', output, flags=re.M):
        match = re.search(r'^\s*pool: (\S+)', block, re.M)
        if not match:
            continue
        pool = match.group(1)

        state = re.search(r'^\s*state: (\S+)', block, re.M)
        if state and state.group(1) != 'ONLINE':
            found.append(('error', f'{pool} {state.group(1)}', 'see zpool status'))

        scan = re.search(r'(scrub|resilver) in progress', block)
        if scan:
            done = re.search(r'([\d.]+)% done', block)
            what = 'scrubbing' if scan.group(1) == 'scrub' else 'resilvering'
            pct = f'{float(done.group(1)):.0f}% done - ' if done else ''
            found.append(('maint', f'{pool} {what}', f'{pct}disks may be slow'))

        # vdev rows under "config:": NAME STATE READ WRITE CKSUM
        config = block.split('config:', 1)[-1].split('errors:', 1)[0]
        for line in config.split('\n'):
            cols = line.split()
            if len(cols) < 5 or cols[0] in ('NAME', pool):
                continue
            read, write, cksum = cols[2:5]
            if not all(re.fullmatch(r'[\d.]+[KMGT]?', c) for c in (read, write, cksum)):
                continue
            if (read, write, cksum) != ('0', '0', '0'):
                disk = names.get(cols[0], cols[0].split('/')[-1])
                found.append(('error', f'{disk} errors',
                              f'read {read} write {write} checksum {cksum}'))

        errors = re.search(r'^errors: (\d+) data errors', block, re.M)
        if errors:
            found.append(('error', f'{pool} data errors',
                          f'{errors.group(1)} - see zpool status -v'))
    return found

def check_smart():
    """One pass over every disk with smartctl; results go to smart_notices."""
    smartctl = shutil.which('smartctl')
    if not smartctl:
        return
    found = []
    for disk in shell('sysctl -n kern.disks').split():
        rc, out = run(f'{smartctl} -H -A /dev/{disk}', timeout=60)
        # "SMART overall-health self-assessment test result: PASSED"
        # (SAS disks say "SMART Health Status: OK"); disks without SMART
        # (USB sticks) have neither line and are skipped
        health = re.search(r'self-assessment test result: (\S+)|'
                           r'SMART Health Status: (\S+)', out)
        if health and (health.group(1) or health.group(2)) not in ('PASSED', 'OK'):
            found.append(('error', f'{disk} SMART', 'health check FAILED'))
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
                          'sectors: ' + ', '.join(bad)))
    with ui_lock:
        smart_notices[:] = found

def smart_loop():
    while True:
        try:
            check_smart()
        except (subprocess.SubprocessError, OSError) as e:
            print(f'SMART check failed: {e}', file=sys.stderr)
        time.sleep(SMART_INTERVAL)

def show_notice():
    if not notices:
        show('Notices', 'none')
        return
    index = nth(show_notice)
    level, line1, line2 = notices[index]
    mark = '!' if level == 'error' else '~'
    show(f'{mark}{line1}', line2)

status_led = None   # what the status LED should show, driven or not
def set_status_led(colour):
    global status_led
    if colour == status_led:
        return
    status_led = colour
    web_notify()
    if args.no_status_led or not os.access(QNAPLED, os.X_OK):
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
            redraw_if(show_updates)

def set_busy(text):
    with ui_lock:
        updates['busy'] = text
        redraw_if(show_updates)

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
        n, osu = updates['pkg'], updates['os']
        parts = ([f'{n} package' + ('s' if n != 1 else '')] if n else []) + \
                (['FreeBSD'] if osu else [])
        line1 = 'Updates: ' + ' + '.join(parts)

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

#
# Menu
#
menu_item = 0
menu = [
    show_truenas,
    show_version,
    show_uptime
]
STATIC_MENU = list(menu)

# what Enter does on a screen; screens not listed ignore it
ACTIONS = {
    show_updates: select_updates,
}

def rebuild_menu():
    """Rebuild the dynamic part of the menu. Repeated screens (one per
    notice, interface, pool) work out which item they are with nth()."""
    global menu_item
    new = list(STATIC_MENU)
    new += [show_notice] * max(1, len(notices))
    if not args.no_updates:
        new.append(show_updates)
    new += [show_ip] * len(ip_addresses)
    new += [show_zpool] * len(zfs_pools)
    menu[:] = new
    menu_item = min(menu_item, len(menu) - 1)

def redraw_if(func):
    """Redraw if `func` is on screen and no prompt is up."""
    if prompt_state is None and menu[menu_item] == func:
        func()

def refresh():
    global menu_item
    ips, links = find_ips()
    pools = find_zpools()
    found = pool_notices()
    with ui_lock:
        old_errors = {n for n in notices if n[0] == 'error'}
        ip_addresses[:] = ips
        lan_links[:] = links
        zfs_pools[:] = pools
        notices[:] = found + smart_notices
        rebuild_menu()
        web_notify()

        if any(n[0] == 'error' for n in notices):
            set_status_led('red')
        elif notices:
            set_status_led('alternate')
        else:
            set_status_led('green')

        new_errors = [n for n in notices if n[0] == 'error' and n not in old_errors]
        if new_errors and prompt_state is None:
            # pop the first new error up on the display
            menu_item = menu.index(show_notice) + notices.index(new_errors[0])
            lcd_on()

        if prompt_state is None:
            menu[menu_item]()

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
            menu[menu_item]()

#
# Buttons, labelled Enter and Select on the TS-x69. The panel reports the
# buttons held after every change as a bitmask, and 0x00 once all are
# released (pressing both reads e.g. 0x02 0x03 0x01 0x00). Act on release:
# Select moves to the next screen, Enter runs the screen's action (or says
# yes to a prompt). Both together does nothing.
#
ENTER = 0x01
SELECT = 0x02
held = 0
buttons_down = 0    # as last reported by the panel, for the web view

def select_pressed():
    global menu_item
    with ui_lock:
        if prompt_state is not None:
            answer(False)
            return
        menu_item = (menu_item + 1) % len(menu)
        menu[menu_item]()

def enter_pressed():
    with ui_lock:
        if prompt_state is not None:
            answer(True)
            return
        action = ACTIONS.get(menu[menu_item])
        if action:
            action()

def response_handler(command, data):
    # runs on the serial reader thread: an exception here would kill it and
    # leave the buttons dead until the service restarts
    try:
        handle_buttons(command, data)
    except Exception as e:
        print(f'button handling failed: {e!r}', file=sys.stderr)

def handle_buttons(command, data):
    global held, buttons_down

    if command != 'Switch_Status':
        return

    with ui_lock:
        lcd_on()
        buttons_down = data & 0x03
        web_notify()
        if data & 0x03:
            held |= data & 0x03
            return

        pressed, held = held, 0
        if pressed == ENTER:
            enter_pressed()
        elif pressed == SELECT:
            select_pressed()

#
# Web panel (--web): a live copy of the display, LEDs and buttons in a
# browser. The display comes from what QnapLCD last sent to the panel.
#
web = None

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
            disks = [d for d in match.group(1).split(',') if re.fullmatch(r'(ada|da)\d+', d)]
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
    errors = {n[1].split()[0] for n in notices if n[0] == 'error'}
    return [{'disk': disk,
             'state': 'empty' if not disk else 'error' if disk in errors else 'ok',
             'busy': disk in disk_busy}
            for disk in bay_disks]

def web_state():
    # no ui_lock: this runs on the web server's threads and only reads
    with lcd.state_lock:
        lines = list(lcd.shown)
    return {
        'host': platform.node().split('.')[0],
        'lines': lines,
        'backlight': lcd.lit,
        'status_led': status_led,
        'status_led_driven': not args.no_status_led and os.access(QNAPLED, os.X_OK),
        'buttons': {'enter': bool(buttons_down & ENTER),
                    'select': bool(buttons_down & SELECT)},
        'prompt': prompt_state is not None,
        'screen': [menu_item + 1, len(menu)],
        'connected': lcd.connection is not None,
        'bays': bay_state(),
        'lan': 'busy' if lan_busy and lan_links else 'on' if lan_links else 'off',
    }

def web_press(name):
    # the same path as the real buttons: press, then release
    bit = ENTER if name == 'enter' else SELECT
    with ui_lock:
        handle_buttons('Switch_Status', bit)
        handle_buttons('Switch_Status', 0x00)

def parse_listen(value):
    """'8080', '127.0.0.1:8080' or '[::]:8080' -> (address, port)."""
    addr, sep, port = value.rpartition(':')
    if not sep:
        addr = '0.0.0.0'
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
    threading.Thread(target=scroll_loop, daemon=True).start()

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

            time.sleep(args.refresh)
    except KeyboardInterrupt:
        pass
    finally:
        if lcd_timer:
            lcd_timer.cancel()
        lcd_off()

if __name__ == '__main__':
    main()
