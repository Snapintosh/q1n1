#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Get the Mac-A16 USB network link back after an A16 reset, without a replug.

Read-only unless told otherwise (docs/A16-USB-REARM.md):

  mac    The Mac's view, from the IORegistry only: each USB-C port's data role
         and whether the Mac draws power there, q1n1's USB devices on the Mac's
         host controllers, and which NCM interfaces can reach q1n1. No request
         goes to the A16 or to any port controller.
  watch  `mac` once a second, printing what changed with UTC timestamps. Run it
         across `a16ctl.py boot q1n1` to record what a reset does to the cable.
  a16    The A16's view over whichever q1n1 endpoint answers: bootinfo, stage
         features, each controller's role, attempts and last error, and the NCM
         counters. Proxy reads only (and the NOP handshake). --portsc also reads
         xHCI PORTSC of a controller that is in the host role (guarded MMIO reads).
  arm    The one step that changes anything: chainload a stage built with
         Q1N1_GADGET_NCM over the endpoint that came back (after a reset that is
         the CDC serial port), wait for q1n1's own NCM interface on the Mac, and
         prove it: generation and EL over UDP, NOPs, and a heap round trip whose
         original bytes are put back. Refuses without --yes, with another
         hardware client running, below 50 % battery without AC, when the image
         is not a gadget-NCM stage, and when a host-role NCM link is already up.

Recovery from any failure of `arm` is today's procedure: `a16ctl.py boot q1n1`
(back to the ESP payload, whose CDC port returns by itself), or a physical
replug of the cable.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import plistlib
import re
import struct
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import udplink  # noqa: E402

A16_SERIALS = ('A16-Q1N1-EL2', 'A16-Q1N1-EL2B', 'A16-Q1N1-BOOT', 'A16-Q1N1-UEFI')
PROXY_SERIALS = ('A16-Q1N1-EL2', 'A16-Q1N1-EL2B')
STAGE_FEATURES_MAGIC = 0x3153544145463151      # "Q1FEATS1" at image offset 48
STAGE_GADGET_NCM = 1 << 1                       # entry.S, Q1N1_GADGET_NCM
ROLES = {0: 'searching', 1: 'host', 2: 'device', 3: 'unsupported'}
# usb-ports.c records what failed in a role attempt. ncm_open's steps are the
# ones that tell a wedged Mac apart: it was connected but would not take an
# address (A16-Q1N1-PROXY.md, "One session per replug").
LAST_ERRORS = {
    -1: 'NCM: no DMA for buffers', -2: 'NCM: no connected port', -3: 'NCM: Address Device failed '
    '(a connected device that will not take an address: the Mac XDCI Full-speed wedge)',
    -4: 'NCM: device descriptor', -5: 'NCM: configuration descriptor', -6: 'NCM: no NCM function',
    -7: 'NCM: SET_CONFIGURATION', -8: 'NCM: Configure Endpoint', -9: 'NCM: SET_INTERFACE',
    -10: 'xHCI did not halt before a role change', -11: 'device run/stop still set',
    -12: 'device stop failed', -13: 'host stop failed after disconnect', -14: 'host stop failed after dwell'}
# Proxy reads over CDC stall after whole multiples of 1020 bytes (A16-Q1N1-PROXY.md,
# "CDC is still capped by macOS"); verify uploads below that on a tty.
CDC_READ_CHUNK = 1008
CLIENT_PATTERN = re.compile(
    r'udp://en\d|/dev/cu\.usbmodem|q1n1proxy\.py|a16gpu\.py|a16ctl\.py|guest-console\.py|cycle\.py|'
    r'request-stop\.py|deploy-guest-plugin\.py|a16-link-rearm\.py')


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds')


def ioreg(cls, run=subprocess.run):
    try:
        result = run(['ioreg', '-a', '-l', '-r', '-c', cls], capture_output=True, timeout=5)
        return plistlib.loads(result.stdout) if result.stdout.strip() else []
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError):
        return []


def children(node):
    for child in node.get('IORegistryEntryChildren', []) if isinstance(node, dict) else []:
        yield child
        yield from children(child)


def interface_state(name, run=subprocess.run):
    try:
        text = run(['ifconfig', name], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return {'present': False}
    return {'present': bool(text), 'active': 'status: active' in text,
            'link_local': bool(re.search(r'inet6 fe80:', text))}


def mac_snapshot(run=subprocess.run):
    """Everything here comes from the IORegistry and ifconfig: no requests."""
    ports = []
    for cls in ('AppleHPMInterfaceType10', 'AppleHPMInterfaceType11'):
        for node in ioreg(cls, run):
            if not isinstance(node, dict) or 'PortNumber' not in node:
                continue
            roles = {c.get('IORegistryEntryName', '?'): c['DataRoleDescription']
                     for c in children(node) if isinstance(c, dict) and 'DataRoleDescription' in c}
            limits = node.get('IOAccessoryPowerCurrentLimits') or []
            # Every USB-C port is "Port-USB-C"; the number tells them apart.
            ports.append({'name': f"{node.get('IORegistryEntryName', 'port')}@{node['PortNumber']}",
                          'port': node['PortNumber'], 'connected': bool(node.get('ConnectionActive')),
                          'partner': node.get('IOAccessoryUSBConnectString'),
                          'data_roles': roles, 'mac_draws_power': any(limits),
                          'connections': node.get('ConnectionCount')})
    devices = []
    for node in ioreg('IOUSBHostDevice', run):
        for candidate in [node] + list(children(node)):
            # The device's interface nubs carry its serial number too.
            if not isinstance(candidate, dict) or 'bInterfaceNumber' in candidate or \
                    candidate.get('IOObjectClass', 'IOUSBHostDevice') != 'IOUSBHostDevice':
                continue
            serial = candidate.get('kUSBSerialNumberString') or candidate.get('USB Serial Number')
            if serial not in A16_SERIALS:
                continue
            interfaces = sorted({(c.get('bInterfaceNumber'), c.get('bInterfaceClass'), c.get('bInterfaceSubClass'))
                                 for c in children(candidate)
                                 if isinstance(c, dict) and 'bInterfaceClass' in c and 'bInterfaceNumber' in c})
            devices.append({'serial': serial, 'product': candidate.get('USB Product Name'),
                            'speed': {0: 'low', 1: 'full', 2: 'high', 3: 'super'}.get(candidate.get('Device Speed'),
                                                                                       candidate.get('Device Speed')),
                            'location': candidate.get('locationID'), 'session': candidate.get('sessionID'),
                            'ncm_function': any(c == 2 and s == 0x0d for _, c, s in interfaces),
                            'gadget_interfaces': udplink.gadget_interface_names([candidate])})
    unique = {(d['serial'], d['location'], d['session']): d for d in devices}
    device_ncm = udplink._inventory('AppleUSBDeviceNCM11Data', udplink.ncm_interface_names)
    gadget_ncm = sorted({n for d in unique.values() for n in d['gadget_interfaces']}, key=lambda n: int(n[2:]))
    states = {name: interface_state(name, run) for name in device_ncm + gadget_ncm}
    return {'time': utc(), 'ports': ports, 'a16_devices': list(unique.values()),
            'device_mode_ncm': {n: states[n] for n in device_ncm}, 'gadget_ncm': {n: states[n] for n in gadget_ncm}}


def classify(snapshot):
    """Which links exist now, most useful first."""
    found = []
    for name, state in snapshot['gadget_ncm'].items():
        if state.get('active'):
            found.append(('gadget-ncm', f'udp://{name}: q1n1 is the USB device and offers NCM; the Mac hosts it'))
    for name, state in snapshot['device_mode_ncm'].items():
        if state.get('active'):
            found.append(('ncm-host', f'udp://{name}: q1n1 is the USB host; the Mac is an NCM device (the replug link)'))
    for device in snapshot['a16_devices']:
        if device['serial'] in PROXY_SERIALS:
            kind = 'with' if device['ncm_function'] else 'without'
            found.append(('mac-is-host', f"{device['serial']} at {device['speed']} speed, {kind} an NCM function: "
                                         'the direction that survives an A16 reset'))
        elif device['serial'] == 'A16-Q1N1-BOOT':
            found.append(('boot-window', 'the q1n1 boot window is waiting on USB0'))
    return found or [('none', 'no q1n1 USB device and no active NCM interface')]


def summary(snapshot):
    """The fields worth comparing from one second to the next."""
    return {'ports': {p['name']: {'connected': p['connected'], 'roles': p['data_roles'],
                                  'mac_draws_power': p['mac_draws_power'], 'connections': p['connections']}
                      for p in snapshot['ports']},
            'a16': {f"{d['serial']}@{d['location']}": {'speed': d['speed'], 'session': d['session'],
                                                      'ncm': d['ncm_function']} for d in snapshot['a16_devices']},
            'ncm': {n: s.get('active') for n, s in {**snapshot['device_mode_ncm'], **snapshot['gadget_ncm']}.items()}}


def changes(before, after, prefix=''):
    out = []
    for key in sorted(set(before) | set(after), key=str):
        a, b = before.get(key), after.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            out += changes(a, b, f'{prefix}{key}.')
        elif a != b:
            out.append(f'{prefix}{key}: {a} -> {b}')
    return out


# ---- the A16's view ----

def unpack(fmt, data):
    return struct.unpack(fmt, data[:struct.calcsize(fmt)])


def stage_has_gadget_ncm(image):
    if len(image) < 64:
        return False
    magic, bits = struct.unpack_from('<QQ', image, 48)
    return magic == STAGE_FEATURES_MAGIC and bool(bits & STAGE_GADGET_NCM)


def a16_snapshot(proxy, portsc=False):
    """Read-only: bootinfo, features, and every published USB counter."""
    info = proxy.bootinfo()
    features = proxy.stage_features()
    record = {'time': utc(), 'endpoint': getattr(proxy, 'endpoint', None),
              'generation': info['stage_generation'], 'el': info['current_el'],
              'image_base': info['image_base'], 'return_armed': info['return_armed'],
              'features': features, 'gadget_ncm_stage': bool(features & STAGE_GADGET_NCM), 'ports': []}
    count = info.get('usb_port_count', 0) if info.get('usb_ports') else 0
    for i in range(min(count, 2)):
        raw = proxy.readmem(info['usb_ports'] + i * 80, 80, live=True)
        base, role, connected, attempts, disconnects, error, device_stats, host_stats, ncm_stats, proxy_stats = \
            unpack('<10Q', raw)
        error = error - (1 << 64) if error >> 63 else error
        port = {'controller': i, 'base': base, 'role': ROLES.get(role, role), 'connected': connected,
                'attempts': attempts, 'disconnects': disconnects, 'last_error': error,
                'last_error_meaning': LAST_ERRORS.get(error, '') if error else ''}
        if device_stats and role == 2:
            # struct qdwc3_stats: 14 u64, 8 u32; the gadget build appends 8 u64.
            size = 144 + (64 if features & STAGE_GADGET_NCM else 0)
            stats = proxy.readmem(device_stats, size, live=True)
            values = unpack('<14Q8I', stats)
            port['device'] = {'setups': values[3], 'resets': values[5], 'connects': values[6],
                              'disconnects': values[7], 'rx_bytes': values[8], 'tx_bytes': values[9],
                              'speed': {0: 'high', 1: 'full', 4: 'super'}.get(values[14], values[14]),
                              'address': values[15], 'configured': values[16]}
            if size > 144:
                port['gadget'] = dict(zip(('requests', 'notifications', 'blocks_in', 'blocks_out', 'empty_blocks',
                                           'alt', 'alt_changes', 'tx_limit'), struct.unpack_from('<8Q', stats, 144)))
        if ncm_stats and (role == 1 or features & STAGE_GADGET_NCM):
            values = unpack('<12QI', proxy.readmem(ncm_stats, 100, live=True))
            port['ncm'] = dict(zip(('blocks_in', 'blocks_out', 'frames_in', 'frames_out', 'bytes_in', 'bytes_out',
                                    'malformed', 'oversize', 'send_failures', 'bad_signature', 'bad_ndp',
                                    'bad_entry', 'init_step'), values))
        if proxy_stats:
            port['udp'] = dict(zip(('frames_in', 'frames_out', 'bytes_in', 'bytes_out', 'dropped', 'foreign',
                                    'solicitations', 'advertisements', 'bad_checksum'),
                                   unpack('<9Q', proxy.readmem(proxy_stats, 72, live=True))))
        if portsc and role == 1 and base:
            # CAPLENGTH, then PORTSC of both ports (USB2, USB3) of this controller.
            operational = base + ((proxy.read32(base) or 0) & 0xff)
            port['portsc'] = [proxy.read32(operational + 0x400 + 0x10 * n) or 0 for n in range(2)]
            port['portsc_speed'] = [{1: 'full', 2: 'low', 3: 'high', 4: 'super'}.get((v >> 10) & 0xf, 'none')
                                    if v & 1 else 'none' for v in port['portsc']]
        record['ports'].append(port)
    return record


# ---- arm ----

class Refused(RuntimeError):
    pass


def other_clients(ps_output, mine):
    """Command lines of other processes that may be talking to the A16."""
    found = []
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid, command = int(parts[0]), parts[2]
        if pid not in mine and CLIENT_PATTERN.search(command):
            found.append(f'{pid}: {command[:160]}')
    return found


def ancestors():
    table, pid, mine = {}, os.getpid(), set()
    out = subprocess.run(['ps', '-axo', 'pid=,ppid=,command='], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = int(parts[1])
    while pid in table and pid not in mine:
        mine.add(pid)
        pid = table[pid]
    return out, mine | {os.getpid()}


def battery_gate(proxy, minimum=50):
    """AGENTS.md: AC, or at least 50 % battery, before any hardware change."""
    try:
        status, battery = proxy.live_battery()
    except Exception as problem:  # noqa: BLE001 -- an older payload has no live path
        return False, f'battery unreadable ({problem})'
    ok = status == 0 and battery.get('plausible') and (battery.get('ac') or battery.get('percent', 0) >= minimum)
    return bool(ok), battery.get('text') or f"status {status}, {battery.get('percent')} %, ac {battery.get('ac')}"


def verified_upload(proxy, base, data, tty):
    """writemem, then read it back in pieces CDC can carry, then the jump."""
    proxy.writemem(base, data)
    chunk = CDC_READ_CHUNK if tty else len(data)
    for offset in range(0, len(data), chunk):
        piece = data[offset:offset + chunk]
        if proxy.readmem(base + offset, len(piece)) != piece:
            raise RuntimeError(f'stage did not verify at {base + offset:#x}')
    proxy.flush_code(base, len(data))
    proxy.request(proxy.P_VECTOR, base, proxy.bootinfo()['address'])


def round_trip(proxy, address, size):
    """Write a pattern, read it back, and put the original bytes back."""
    original = proxy.readmem(address, size)
    pattern = bytes((i * 37 + 11) & 0xff for i in range(size))
    start = time.monotonic()
    proxy.writemem(address, pattern)
    wrote = time.monotonic() - start
    start = time.monotonic()
    back = proxy.readmem(address, size)
    read = time.monotonic() - start
    proxy.writemem(address, original)
    restored = proxy.readmem(address, size) == original
    return {'bytes': size, 'match': back == pattern, 'restored': restored,
            'write_mb_s': round(size / wrote / 1e6, 2) if wrote else None,
            'read_mb_s': round(size / read / 1e6, 2) if read else None}


def arm(args, q, record):
    if not args.yes:
        raise Refused('arm changes the running stage; pass --yes to do it')
    ps_out, mine = ancestors()
    clients = other_clients(ps_out, mine)
    if clients:
        raise Refused('another hardware client is running:\n  ' + '\n  '.join(clients))
    image = Path(args.stage).read_bytes()
    if not stage_has_gadget_ncm(image):
        raise Refused(f'{args.stage} is not a Q1N1_GADGET_NCM stage (no feature bit 1 at offset 56)')
    record['mac_before'] = mac_snapshot()
    proxy = q.connect(args.device, wait=args.connect_wait)
    tty = not str(proxy.endpoint).startswith('udp://')
    record['a16_before'] = before = a16_snapshot(proxy)
    if before['el'] != 2:
        raise Refused(f"the target is at EL{before['el']}, not the EL2 proxy")
    if any(p['role'] == 'host' and p['connected'] for p in before['ports']):
        raise Refused('a host-role NCM link is up already; nothing to re-arm (and a new layout would reset it)')
    ok, battery = battery_gate(proxy)
    record['battery'] = battery
    if not ok and not args.battery_ok:
        raise Refused(f'battery check failed: {battery}; plug in AC (or pass --battery-ok after checking it)')
    chosen, data, link = proxy.pick_stage(Path(args.stage))
    if not stage_has_gadget_ncm(data):
        raise Refused(f'{chosen} (the free slot) is not a Q1N1_GADGET_NCM stage')
    proxy.check_stage_transport(data, proxy.bootinfo())
    print(f'{utc()} chainloading {chosen.name} ({len(data)} bytes) to {link:#x} over {proxy.endpoint}')
    start = time.monotonic()
    verified_upload(proxy, link, data, tty)
    record['chainload'] = {'stage': str(chosen), 'link': link, 'bytes': len(data),
                           'seconds': round(time.monotonic() - start, 2), 'endpoint': proxy.endpoint}
    proxy.link.close()
    want = before['generation'] + 1
    deadline = time.monotonic() + args.wait
    interface = None
    while time.monotonic() < deadline:
        snapshot = mac_snapshot()
        active = [n for n, s in snapshot['gadget_ncm'].items() if s.get('active')]
        if active:
            interface = active[0]
            record['mac_after'] = snapshot
            break
        time.sleep(0.5)
    record['ncm_interface'] = interface
    if not interface:
        record['mac_after'] = mac_snapshot()
        try:
            fresh = q.connect(None, wait=5)
            record['a16_after'] = a16_snapshot(fresh)
            fresh.link.close()
        except Exception as problem:  # noqa: BLE001
            record['a16_after'] = {'error': str(problem)}
        raise RuntimeError(f'no active q1n1 NCM interface within {args.wait:.0f} s')
    print(f'{utc()} q1n1 NCM interface {interface} is active; verifying over udp://{interface}')
    fresh = q.connect(f'udp://{interface}', wait=args.connect_wait)
    after = record['a16_after'] = a16_snapshot(fresh)
    if after['generation'] != want or after['image_base'] != link or after['el'] != 2:
        raise RuntimeError(f"udp://{interface} answers generation {after['generation']} at {after['image_base']:#x}, "
                           f'expected {want} at {link:#x}')
    start = time.monotonic()
    for _ in range(200):
        fresh.nop()
    record['nops'] = {'count': 200, 'seconds': round(time.monotonic() - start, 3)}
    info = fresh.bootinfo()
    record['round_trip'] = round_trip(fresh, info['heap_base'], args.transfer_bytes)
    fresh.link.close()
    if not (record['round_trip']['match'] and record['round_trip']['restored']):
        raise RuntimeError(f"heap round trip failed: {record['round_trip']}")
    return interface


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('mac', help='read-only: the Mac side, from the IORegistry')
    watch = sub.add_parser('watch', help='read-only: print Mac-side changes once a second')
    watch.add_argument('--seconds', type=float, default=180)
    a16 = sub.add_parser('a16', help='read-only: the A16 side over a q1n1 endpoint')
    a16.add_argument('--device', help='tty or udp://enN (default: the first q1n1 endpoint that answers)')
    a16.add_argument('--portsc', action='store_true', help='also read xHCI PORTSC of host-role controllers')
    a16.add_argument('--wait', type=float, default=10)
    armed = sub.add_parser('arm', help='chainload a gadget-NCM stage and verify the NCM link (needs --yes)')
    armed.add_argument('stage', help='build/uefi-gadget-ncm/q1n1-stage.bin (its -alt sibling is picked as needed)')
    armed.add_argument('--device', help='the endpoint to chainload over, e.g. /dev/cu.usbmodemA16_Q1N1_EL2B1')
    armed.add_argument('--yes', action='store_true')
    armed.add_argument('--wait', type=float, default=90, help='seconds for the NCM interface to come up')
    armed.add_argument('--connect-wait', type=float, default=30)
    armed.add_argument('--transfer-bytes', type=lambda v: int(v, 0), default=1 << 20)
    armed.add_argument('--battery-ok', action='store_true',
                       help='the payload cannot read the battery and the operator confirmed AC or >= 50 %%')
    for p in (sub.choices['mac'], watch, a16, armed):
        p.add_argument('--record', type=Path, help='write everything observed to this JSON file')
    args = parser.parse_args()
    if args.command == 'arm' and not 4096 <= args.transfer_bytes <= (4 << 20):
        parser.error('--transfer-bytes must be 4 KiB .. 4 MiB')

    record = {'command': args.command, 'started': utc()}
    status = 0
    try:
        if args.command == 'mac':
            snapshot = record['mac'] = mac_snapshot()
            print(json.dumps(snapshot, indent=2))
            for kind, text in classify(snapshot):
                print(f'{kind:12} {text}')
        elif args.command == 'watch':
            previous = summary(mac_snapshot())
            print(f'{utc()} watching; initial state:')
            print(json.dumps(previous, indent=2))
            record['events'] = []
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                time.sleep(1)
                snapshot = mac_snapshot()
                current = summary(snapshot)
                for line in changes(previous, current):
                    stamp = f'{snapshot["time"]} {line}'
                    print(stamp, flush=True)
                    record['events'].append(stamp)
                previous = current
        elif args.command == 'a16':
            import q1n1proxy as q
            proxy = q.connect(args.device, wait=args.wait)
            try:
                snapshot = record['a16'] = a16_snapshot(proxy, args.portsc)
            finally:
                proxy.link.close()
            print(json.dumps(snapshot, indent=2))
        else:
            import q1n1proxy as q
            interface = arm(args, q, record)
            record['result'] = f'PASS: q1n1 NCM link up on udp://{interface}'
            print(record['result'])
            print(f'next: pass --device udp://{interface} to cycle.py (prepare with that device), or run as before')
    except Refused as refusal:
        record['result'] = f'REFUSED: {refusal}'
        print(record['result'], file=sys.stderr)
        status = 2
    except Exception as problem:  # noqa: BLE001 -- record whatever went wrong, then say how to recover
        record['result'] = f'FAIL: {problem}'
        print(record['result'], file=sys.stderr)
        if args.command == 'arm':
            print('recover: python3 tools/a16ctl.py boot q1n1 (the ESP payload brings its CDC port back), '
                  'or replug the cable', file=sys.stderr)
        status = 3
    finally:
        record['finished'] = utc()
        if getattr(args, 'record', None):
            args.record.parent.mkdir(parents=True, exist_ok=True)
            args.record.write_text(json.dumps(record, indent=2, default=str) + '\n')
    return status


if __name__ == '__main__':
    sys.exit(main())
