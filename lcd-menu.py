#!/usr/bin/env python3
import sys
import os
import time
import qnaplcd
import platform
import subprocess
import socket
import threading
import json
import re
import shutil
import signal
import argparse

DISPLAY_TIMEOUT = 30    # seconds

PORT = os.environ.get('QNAPLCD_PORT', qnaplcd.DEFAULT_PORT)
PORT_SPEED = 1200

lcd = None

lcd_timer = None
def lcd_on():
    global lcd_timer

    lcd.backlight(True)

    if lcd_timer:
        lcd_timer.cancel()

    lcd_timer = threading.Timer(DISPLAY_TIMEOUT, lambda: lcd.backlight(False))
    lcd_timer.daemon = True
    lcd_timer.start()

def shell(cmd):
    return subprocess.check_output(cmd, shell=True, universal_newlines=True).strip()

def show_version():
    sys_name = platform.node()
    sys_vers = f'{platform.system()} ({platform.machine()})'
    lcd.clear()
    lcd.write(0, [sys_name, sys_vers])

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

    lcd.clear()
    lcd.write(0, [line1, line2])

def show_uptime():
    uptime = shell('uptime').split(',')
    up = ' '.join(uptime[0].split()[2:]) + ' ' + uptime[1]
    load = os.getloadavg()
    lcd.clear()
    lcd.write(0, [f'Up  : {up}', f'Load: {load[0]} {load[1]}, {load[2]}'])

ip_addresses = []
def add_ips_to_menu():
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

    ip_addresses.clear()
    for name in order:
        if name == 'lo0':
            continue

        iface = interfaces[name]

        # lagg (FreeBSD bonding) members carry no IP of their own -
        # show link status instead, matching a Linux bond slave.
        if iface['is_member']:
            ip_addresses.append((name, iface['status']))
        else:
            ip_addresses.append((name, iface['addr'] or '0.0.0.0'))

    while show_ip in menu:
        menu.remove(show_ip)

    for _ in ip_addresses:
        menu.append(show_ip)

def show_ip():
    ip_index = 0
    for index in range(menu_item):
        if menu[index] == show_ip:
            ip_index += 1

    lcd.clear()
    lcd.write(0, [f'{ip_addresses[ip_index][0]}', f'{ip_addresses[ip_index][1]}'])

zfs_pools = []
def add_zpools_to_menu():
    pools = shell('zpool list').split('\n')

    zfs_pools.clear()
    for pool in pools[1:]:
        zfs_pools.append(pool.split())

    # remove existing zfs pool menu items
    while show_zpool in menu:
        menu.remove(show_zpool)

    # add zfs pool menu items for discovered pools
    for _ in zfs_pools:
        menu.append(show_zpool)

def show_zpool():
    pool_index = 0
    for index in range(menu_item):
        if menu[index] == show_zpool:
            pool_index += 1

    pool = zfs_pools[pool_index]

    lcd.clear()
    lcd.write(0, [f'{pool[0]} ({pool[7]})', f'{pool[2]} of {pool[1]}'])

#
# Menu
#
menu_item = 0
menu = [
    show_truenas,
    show_version,
    show_uptime
]

def response_handler(command, data):
    global menu_item, lcd_timeout
    prev_menu = menu_item

    #print(f'RECV: {command} - {data:#04x}')

    if command == 'Switch_Status':
        lcd_on()

        if data == 0x01: # up
            menu_item = (menu_item - 1) % len(menu)

        if data == 0x02: # down
            menu_item = (menu_item + 1) % len(menu)

    if prev_menu != menu_item:
        #print(f'SHOW: {menu_item}')
        menu[menu_item]()


def handle_sigterm(signum, frame):
    # rc.d stop (via daemon(8)) sends SIGTERM; unwind through main()'s finally
    sys.exit(0)

def main():
    global lcd

    parser = argparse.ArgumentParser(description='QNAP front-panel LCD menu')
    parser.add_argument('--port', default=PORT, help=f'serial device (default {PORT})')
    parser.add_argument('--speed', type=int, default=PORT_SPEED, help=f'baud rate (default {PORT_SPEED})')
    parser.add_argument('--refresh', type=int, default=30, help='seconds between interface/pool rescans (default 30)')
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_sigterm)

    lcd = qnaplcd.QnapLCD(args.port, args.speed, response_handler)
    lcd_on()
    lcd.reset()
    lcd.clear()

    lcd.write(0, [platform.node(), 'System Ready...'])

    try:
        while True:
            try:
                add_ips_to_menu()
                add_zpools_to_menu()
                menu[menu_item]()
            except (subprocess.CalledProcessError, OSError, IndexError) as e:
                # a failing shell command must not take the service down
                print(f'refresh failed: {e}', file=sys.stderr)

            time.sleep(args.refresh)
    except KeyboardInterrupt:
        pass
    finally:
        if lcd_timer:
            lcd_timer.cancel()
        lcd.backlight(False)

if __name__ == '__main__':
    main()
