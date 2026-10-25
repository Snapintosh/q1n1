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
  stress Mixed bulk writes with read-back over udp://enN (64 MiB by default),
         with pauses that used to land a write in a discovery stall, into
         firmware-reported free memory only. Any timeout or mismatch fails it.
         Records q1n1's stall and loss counters and the Mac's drop counters
         before and after. --replay LAUNCH_JSON instead writes the first 90
         uploads of a real boot back to back, with no read-back until the end
         (the pass criterion for a desktop boot); --nak-hold MS,... makes the
         stage NAK the Mac for that long during a 1 MiB write, to see whether
         the Mac's queue drops. Needs --yes.
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
# Where a gadget stage keeps its NCM counters. Bit 3: in a table named at
# image offset 64 (qcom-dwc3.h qdwc3_ncm_table), outside the USB state. Bits 1
# and 2 without 3 are the two earlier layouts, appended to struct qdwc3_stats
# (8 and 16 u64); they changed the handoff layout and are only read, never built.
STAGE_NCM_COUNTERS = 1 << 2
STAGE_NCM_TABLE = 1 << 3
NCM_TABLE_MAGIC = 0x3142544d434e3151           # "Q1NCMTB1"
# qcom-dwc3.h struct qdwc3_ncm_stats. Table version 1 has the first 16;
# version 2 adds the OUT pool and which-side-stalled counters.
GADGET_FIELDS = ('requests notifications blocks_in blocks_out empty_blocks alt alt_changes tx_limit '
                 'out_not_ready rx_chained rx_trailing tx_waits tx_wait_max_us service_gap_max_us '
                 'service_gaps_20ms probes_deferred rx_queue_max rx_queue_full host_wait_max_us '
                 'host_waits_100ms host_waits_1s holds').split()
GADGET_FIELDS_V1 = 16
# usb-ports.h struct q1n1_loop_stats, named by table version 2.
LOOP_FIELDS = ('panel_redraws panel_max_us panels_deferred battery_max_us maintain_max_us '
               'hold_request_ms hold_last_us').split()
LOOP_HOLD_REQUEST = 8 * LOOP_FIELDS.index('hold_request_ms')
# Not differences: the largest value seen, kept as is in a stress's deltas.
PEAKS = ('rx_queue_max',)
# q1n1-proxy.h q1n1_proxy_stats; `timeouts` counts requests whose data stopped
# arriving: what a datagram lost on the way to the A16 looks like from there.
PROXY_FIELDS = ('requests proxy_calls memreads memwrites read_bytes write_bytes checksum_errors '
                'timeouts bad_commands exceptions announcements last_opcode').split()
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
    tables, gadget_fields = {}, GADGET_FIELDS_V1
    if features & STAGE_NCM_TABLE:
        magic, table = unpack('<QQ', proxy.readmem(info['image_base'] + 64, 16))
        if magic == NCM_TABLE_MAGIC and table:
            magic, version, functions = unpack('<3Q', proxy.readmem(table, 24, live=True))
            if magic == NCM_TABLE_MAGIC and version in (1, 2) and functions <= 8:
                pairs = unpack(f'<{2 * functions}Q', proxy.readmem(table + 24, 16 * functions, live=True))
                tables = {pairs[k]: pairs[k + 1] for k in range(0, len(pairs), 2) if pairs[k + 1]}
                record['ncm_table'] = {'address': table, 'version': version}
                if version >= 2:
                    gadget_fields = len(GADGET_FIELDS)
                    loop = unpack('<Q', proxy.readmem(table + 24 + 16 * functions, 8, live=True))[0]
                    if loop:
                        record['ncm_table']['loop'] = loop
                        record['loop'] = dict(zip(LOOP_FIELDS, unpack(f'<{len(LOOP_FIELDS)}Q', proxy.readmem(
                            loop, 8 * len(LOOP_FIELDS), live=True))))
    for i in range(min(count, 2)):
        raw = proxy.readmem(info['usb_ports'] + i * 80, 80, live=True)
        base, role, connected, attempts, disconnects, error, device_stats, host_stats, ncm_stats, proxy_stats = \
            unpack('<10Q', raw)
        error = error - (1 << 64) if error >> 63 else error
        port = {'controller': i, 'base': base, 'role': ROLES.get(role, role), 'connected': connected,
                'attempts': attempts, 'disconnects': disconnects, 'last_error': error,
                'last_error_meaning': LAST_ERRORS.get(error, '') if error else ''}
        if device_stats and role == 2:
            # struct qdwc3_stats: 14 u64, 8 u32; the two retired gadget layouts
            # appended 8 or 16 u64 to it.
            appended = 0
            if features & STAGE_GADGET_NCM and not features & STAGE_NCM_TABLE:
                appended = 16 if features & STAGE_NCM_COUNTERS else 8
            size = 144 + 8 * appended
            stats = proxy.readmem(device_stats, size, live=True)
            values = unpack('<14Q8I', stats)
            port['device'] = {'setups': values[3], 'resets': values[5], 'connects': values[6],
                              'disconnects': values[7], 'rx_bytes': values[8], 'tx_bytes': values[9],
                              'command_failures': values[11], 'dma_mismatch': values[12],
                              'event_overflows': values[13],
                              'speed': {0: 'high', 1: 'full', 4: 'super'}.get(values[14], values[14]),
                              'address': values[15], 'configured': values[16]}
            if appended:
                port['gadget'] = dict(zip(GADGET_FIELDS, struct.unpack_from(f'<{appended}Q', stats, 144)))
            elif base in tables:
                port['gadget'] = dict(zip(GADGET_FIELDS, unpack(f'<{gadget_fields}Q', proxy.readmem(
                    tables[base], 8 * gadget_fields, live=True))))
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
    if info.get('proxy_stats'):
        record['proxy'] = dict(zip(PROXY_FIELDS, unpack('<12Q', proxy.readmem(info['proxy_stats'], 96, live=True))))
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


def stage_contract(image):
    """The 24-byte USB handoff contract a stage image names (header offset 40)."""
    if len(image) < 48 or struct.unpack_from('<Q', image, 32)[0] != 0x3154525055533151:
        return None
    offset = struct.unpack_from('<Q', image, 40)[0] - struct.unpack_from('<Q', image, 16)[0]
    return image[offset:offset + 24] if 48 <= offset <= len(image) - 24 else None


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
    # The reader compares these only while a port is in its host role, which an
    # idle port is now and then; compare them every time, before anything moves.
    info = proxy.bootinfo()
    if info.get('usb_ports'):
        running = proxy.readmem(info['usb_ports'] - 24, 24, live=True)
        if stage_contract(data) != running:
            raise Refused(f'{chosen.name} changes the USB handoff layout of the running payload '
                          f'({struct.unpack("<3Q", running)}); it cannot be armed from it')
    proxy.check_stage_transport(data, info)
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


# ---- stress ----

# The upload sizes that failed on hardware: an 8 MiB and a 2 MiB write, and the
# 13,942,784-byte image after which a desktop boot's next reply never came.
STRESS_SIZES = (8 << 20, 2 << 20, 13942784, 1 << 20, 4096, 8 << 20, 300000, 2 << 20)
RESERVED = (('image_base', 'image_size'), ('heap_base', 'heap_size'), ('dma_base', 'dma_size'),
            ('stage_base', 'stage_size'), ('fb_base', 'fb_size'), ('gpu_arena_base', 'gpu_arena_size'),
            ('preload_table', 'preload_size'))


def free_spans(memory_map, stride, info):
    """Firmware-reported conventional memory (EFI type 7) minus everything q1n1
    itself occupies: the only memory a stress may scribble on at idle."""
    if stride < 40 or len(memory_map) % stride:
        raise ValueError('invalid firmware memory map')
    spans = []
    for at in range(0, len(memory_map), stride):
        kind, _, base, _, pages, _ = struct.unpack_from('<IIQQQQ', memory_map, at)
        if kind == 7 and pages:
            spans.append((base, base + pages * 4096))
    for b, z in RESERVED:
        low, high = info.get(b) or 0, (info.get(b) or 0) + (info.get(z) or 0)
        if not high > low:
            continue
        spans = [piece for begin, end in spans for piece in
                 (((begin, end),) if high <= begin or low >= end else
                  tuple(x for x in ((begin, low), (high, end)) if x[1] > x[0]))]
    return spans


def stress_window(proxy, info, size, address=None):
    blob = proxy.readmem(info['memory_map'], info['memory_map_size'])
    spans = free_spans(blob, info['memory_map_stride'] or 48, info)
    if address is None:
        begin, end = max(spans, key=lambda s: s[1] - s[0])
        address = (begin + (2 << 20) - 1) & ~((2 << 20) - 1)
    if not any(begin <= address and address + size <= end for begin, end in spans):
        raise Refused(f'{address:#x}+{size:#x} is not inside firmware-reported free memory')
    return address


QUEUE_LENGTH = re.compile(r'qlength:\s*(\d+)\s*/\s*(\d+)')
QUEUE_DROPPED = re.compile(r'dropped pkts:\s*(\d+)')


def mac_counters(interface, run=subprocess.run):
    """The Mac's own view of drops: per-interface errors and drops, its send
    queue (length, limit, drops), IPv6 output drops, and UDP datagrams it threw
    away for want of socket buffer (all system-wide but the first two)."""
    record = {}
    try:
        lines = run(['netstat', '-I', interface, '-b', '-d'], capture_output=True, text=True, timeout=5).stdout
        rows = [line.split() for line in lines.splitlines()[1:] if line.split() and line.split()[0] == interface]
        if rows:
            row = rows[0]
            record.update(ipkts=int(row[-8]), ierrs=int(row[-7]), opkts=int(row[-5]), oerrs=int(row[-4]),
                          drop=int(row[-1]))
        text = run(['netstat', '-s', '-p', 'udp'], capture_output=True, text=True, timeout=5).stdout
        found = re.search(r'(\d+) dropped due to full socket buffers', text)
        record['udp_full_socket_drops'] = int(found.group(1)) if found else None
        text = run(['netstat', '-s', '-p', 'ip6'], capture_output=True, text=True, timeout=5).stdout
        found = re.search(r'(\d+) output packets? dropped due to no bufs', text)
        record['ip6_output_no_bufs'] = int(found.group(1)) if found else None
        queue = run(['netstat', '-qq', '-I', interface], capture_output=True, text=True, timeout=5).stdout
        found, dropped = QUEUE_LENGTH.search(queue), QUEUE_DROPPED.search(queue)
        if found:
            record['queue_length'], record['queue_limit'] = int(found.group(1)), int(found.group(2))
        if dropped:
            record['queue_dropped'] = int(dropped.group(1))
        record['queue'] = queue[:4000]          # the summary and the first classes
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as problem:
        record['error'] = str(problem)
    return record


MAC_DELTAS = ('ierrs', 'oerrs', 'drop', 'udp_full_socket_drops', 'ip6_output_no_bufs', 'queue_dropped')


def counter_deltas(before, after):
    """What changed on both sides. Counters are differences; maxima (`*_max_us`,
    PEAKS) are the value after, since they only ever grow."""
    def diff(b, a, skip=()):
        out = {k: v - b.get(k, 0) for k, v in a.items()
               if isinstance(v, int) and k not in skip and not k.endswith('_max_us') and k not in PEAKS}
        out.update({k: v for k, v in a.items() if k.endswith('_max_us') or k in PEAKS})
        return out
    deltas = {'proxy': {k: after.get('proxy', {}).get(k, 0) - before.get('proxy', {}).get(k, 0)
                        for k in ('timeouts', 'checksum_errors') if k in after.get('proxy', {})}}
    for i, (b, a) in enumerate(zip(before['ports'], after['ports'])):
        for key in ('gadget', 'ncm', 'udp'):
            if key in a:
                deltas[f'usb{i}.{key}'] = diff(b.get(key, {}), a[key], ('alt', 'tx_limit', 'init_step'))
        if 'device' in a:
            deltas[f'usb{i}.device'] = {k: a['device'][k] - b.get('device', {}).get(k, 0)
                                        for k in ('resets', 'disconnects', 'command_failures', 'dma_mismatch',
                                                  'event_overflows') if k in a['device']}
    if 'loop' in after:
        deltas['loop'] = diff(before.get('loop', {}), after['loop'], ('hold_request_ms', 'hold_last_us'))
    return deltas


def mac_deltas(before, after):
    return {k: after[k] - before[k] for k in MAC_DELTAS
            if isinstance(after.get(k), int) and isinstance(before.get(k), int)}


def transport_stalls(proxy):
    transport = proxy.link.transport
    return {'send_stalls': getattr(transport, 'send_stalls', None),
            'send_stall_max_s': round(getattr(transport, 'send_stall_max_s', 0.0), 4)}


# ---- boot replay ----

def load_uploads(path, count):
    """(address, size) of the first `count` uploads a launch record lists."""
    record = json.loads(Path(path).read_text())
    rows = record.get('uploads') if isinstance(record, dict) else record
    if not isinstance(rows, list) or not rows:
        raise Refused(f'{path} lists no uploads')
    uploads = []
    for row in rows[:count]:
        address = row['address']
        address = int(address, 0) if isinstance(address, str) else int(address)
        size = int(row['bytes'] if 'bytes' in row else row['size'])
        if size <= 0:
            raise Refused(f'{path}: upload of {size} bytes at {address:#x}')
        uploads.append((address, size))
    return uploads


def page_up(value, page=4096):
    return (value + page - 1) & ~(page - 1)


def plan_replay(uploads, spans):
    """Where to write each upload. One that lies wholly in free memory keeps
    its own address (the boot's own layout, overlaps included); the rest go,
    in order and page-aligned, into the largest stretch of free memory that no
    kept upload touches. Returns (planned, kept_count, relocation_base)."""
    kept = [any(b <= a and a + n <= e for b, e in spans) for a, n in uploads]
    need = sum(page_up(n) for (a, n), k in zip(uploads, kept) if not k)
    gaps = []
    for begin, end in spans:
        cursor = begin
        for low, high in sorted((a, a + n) for (a, n), k in zip(uploads, kept) if k and begin <= a < end):
            if low > cursor:
                gaps.append((cursor, low))
            cursor = max(cursor, high)
        if end > cursor:
            gaps.append((cursor, end))
    base = None
    if need:
        begin, end = max(gaps, key=lambda g: g[1] - g[0]) if gaps else (0, 0)
        base = (begin + (2 << 20) - 1) & ~((2 << 20) - 1)
        if base + need > end:
            raise Refused(f'no free stretch holds the {need:#x} bytes of uploads outside free memory')
    planned, offset = [], 0
    for (a, n), k in zip(uploads, kept):
        if k:
            planned.append((a, n))
        else:
            planned.append((base + offset, n))
            offset += page_up(n)
    return planned, sum(kept), base


def last_writers(planned):
    """Indices of uploads that no later upload overlaps: only those still hold
    their own bytes when the replay ends."""
    keep, covered = [], []
    for i in range(len(planned) - 1, -1, -1):
        a, n = planned[i]
        if not any(a < high and low < a + n for low, high in covered):
            keep.append(i)
        covered.append((a, a + n))
    return sorted(keep)


def replay(args, proxy, info, record):
    uploads = load_uploads(args.replay, args.replay_count)
    blob = proxy.readmem(info['memory_map'], info['memory_map_size'])
    spans = free_spans(blob, info['memory_map_stride'] or 48, info)
    planned, kept, base = plan_replay(uploads, spans)
    total = sum(n for _, n in planned)
    record['replay'] = {'source_uploads': len(uploads), 'bytes': total, 'kept_addresses': kept,
                        'relocated_to': base}
    print(f'{utc()} replay: {len(planned)} uploads, {total} bytes, back to back with no read-back; '
          f'{kept} at their own address, the rest from {base:#x}' if base is not None else
          f'{utc()} replay: {len(planned)} uploads, {total} bytes, all at their own address', flush=True)
    data, ops, failures = [], [], 0
    for i, (address, size) in enumerate(planned):
        payload = os.urandom(size)
        data.append(payload)
        op = {'index': i, 'address': address, 'size': size}
        start = time.monotonic()
        try:
            proxy.writemem(address, payload)
            op['write_s'] = round(time.monotonic() - start, 4)
        except Exception as problem:  # noqa: BLE001 -- the failure is the measurement
            op['error'] = f'{type(problem).__name__}: {problem}'
            op['after_s'] = round(time.monotonic() - start, 4)
            failures += 1
            ops.append(op)
            print(f'{utc()} upload {i} ({size} bytes at {address:#x}): {op["error"]}', flush=True)
            break
        ops.append(op)
        if size >= 1 << 20:
            print(f"{utc()} upload {i}: {size:>9} bytes  {op['write_s']} s  "
                  f"{size / max(op['write_s'], 1e-6) / 1e6:.1f} MB/s", flush=True)
    done = [o for o in ops if 'write_s' in o]
    seconds = sum(o['write_s'] for o in done)
    written = sum(o['size'] for o in done)
    summary = {'uploads': len(done), 'of': len(planned), 'bytes': written, 'failures': failures,
               'seconds': round(seconds, 3), 'write_mb_s': round(written / seconds / 1e6, 2) if seconds else None,
               'slowest': max(done, key=lambda o: o['write_s'] / o['size'])['index'] if done else None}
    if not failures and args.verify:
        mismatched = []
        for i in last_writers(planned):
            address, size = planned[i]
            if proxy.readmem(address, size) != data[i]:
                mismatched.append(i)
        summary['verified'] = len(last_writers(planned)) - len(mismatched)
        summary['mismatched'] = mismatched
        failures += len(mismatched)
        summary['failures'] = failures
    record['ops'] = ops
    return summary, failures


# ---- the NAK probe ----

def nak_probe(args, proxy, info, record, address):
    """Hold the stage's OUT endpoint unarmed for each requested time while a
    1 MiB write is in flight, then read it back. The Mac has to queue the
    write meanwhile; a timeout or mismatch is its queue dropping."""
    loop = record['a16_before'].get('ncm_table', {}).get('loop')
    if not loop:
        raise Refused('the running stage publishes no NAK-probe control (ncm table version 2)')
    rows, failures = [], 0
    for ms in args.nak_hold:
        payload = os.urandom(args.nak_bytes)
        row = {'hold_ms': ms, 'size': args.nak_bytes}
        proxy.writemem(loop + LOOP_HOLD_REQUEST, struct.pack('<Q', ms))
        start = time.monotonic()
        try:
            proxy.writemem(address, payload)
            row['write_s'] = round(time.monotonic() - start, 4)
            row['match'] = proxy.readmem(address, args.nak_bytes) == payload
            failures += not row['match']
        except Exception as problem:  # noqa: BLE001
            row['error'] = f'{type(problem).__name__}: {problem}'
            failures += 1
            try:
                proxy.link.resync(args.nak_bytes + 4)
            except Exception as again:  # noqa: BLE001
                row['resync_error'] = str(again)
                rows.append(row)
                break
        loop_now = dict(zip(LOOP_FIELDS, unpack(f'<{len(LOOP_FIELDS)}Q',
                                                 proxy.readmem(loop, 8 * len(LOOP_FIELDS), live=True))))
        row['hold_last_us'] = loop_now['hold_last_us']
        rows.append(row)
        print(f"{utc()} hold {ms} ms: write {row.get('write_s', '-')} s, held {row['hold_last_us']} us, "
              f"{'intact' if row.get('match') else row.get('error', 'MISMATCH')}", flush=True)
    record['nak_probe'] = rows
    summary = {'holds': len(rows), 'failures': failures,
               'longest_intact_ms': max([r['hold_ms'] for r in rows if r.get('match')], default=None)}
    return summary, failures


# ---- stress ----

def mixed(args, proxy, record, address):
    ops, moved, failures = [], 0, 0
    print(f'{utc()} stress: {args.total >> 20} MiB in mixed writes with read-back at {address:#x}, '
          f'pause {args.pause} s before every third, retries {args.retries}')
    while moved < args.total and failures < 3:
        size = STRESS_SIZES[len(ops) % len(STRESS_SIZES)]
        if args.pause and len(ops) % 3 == 2:
            time.sleep(args.pause)       # a pause before a write is what used to land in a stall
        data = os.urandom(size)
        op = {'size': size}
        try:
            start = time.monotonic()
            proxy.writemem(address, data)
            op['write_s'] = round(time.monotonic() - start, 4)
            start = time.monotonic()
            back = proxy.readmem(address, size)
            op['read_s'] = round(time.monotonic() - start, 4)
            op['match'] = back == data
            if not op['match']:
                failures += 1
        except Exception as problem:  # noqa: BLE001 -- a timeout is the result being measured
            failures += 1
            op['error'] = f'{type(problem).__name__}: {problem}'
            print(f'{utc()} op {len(ops)} ({size} bytes): {op["error"]}; resynchronising', flush=True)
            try:
                proxy.link.resync(size + 4)
            except Exception as again:  # noqa: BLE001
                op['resync_error'] = str(again)
                ops.append(op)
                break
        ops.append(op)
        moved += size
        print(f"{utc()} op {len(ops) - 1}: {size:>9} bytes  write {op.get('write_s', '-')} s  "
              f"read {op.get('read_s', '-')} s  {'ok' if op.get('match') else 'FAIL'}", flush=True)
    record['ops'] = ops
    good = [o for o in ops if o.get('match')]
    written = sum(o['size'] for o in good)
    summary = {
        'ops': len(ops), 'bytes': written, 'failures': failures,
        'timeouts': sum('Timeout' in o.get('error', '') for o in ops),
        'write_mb_s': round(written / sum(o['write_s'] for o in good) / 1e6, 2) if good else None,
        'read_mb_s': round(written / sum(o['read_s'] for o in good) / 1e6, 2) if good else None}
    return summary, failures


def stress(args, q, record):
    if not args.yes:
        raise Refused('stress writes target memory; pass --yes')
    if not str(args.device).startswith('udp://'):
        raise Refused('stress is for a udp://enN link')
    ps_out, mine = ancestors()
    clients = other_clients(ps_out, mine)
    if clients:
        raise Refused('another hardware client is running:\n  ' + '\n  '.join(clients))
    proxy = q.connect(args.device, wait=args.connect_wait)
    proxy.link.loss_retries = args.retries
    info = proxy.bootinfo()
    if info['current_el'] != 2:
        raise Refused(f"the target is at EL{info['current_el']}")
    ok, battery = battery_gate(proxy)
    record['battery'] = battery
    if not ok and not args.battery_ok:
        raise Refused(f'battery check failed: {battery}; plug in AC (or pass --battery-ok after checking it)')
    mode = 'replay' if args.replay else 'nak-probe' if args.nak_hold else 'mixed'
    need = args.nak_bytes if mode == 'nak-probe' else max(STRESS_SIZES)
    address = None if mode == 'replay' else stress_window(proxy, info, need, args.address)
    interface = str(args.device)[6:]
    record.update(mode=mode, address=address, retries=args.retries, pause_s=args.pause,
                  window=proxy.write_chunk(), mac_before=mac_counters(interface), a16_before=a16_snapshot(proxy))
    if mode == 'replay':
        summary, failures = replay(args, proxy, info, record)
    elif mode == 'nak-probe':
        summary, failures = nak_probe(args, proxy, info, record, address)
    else:
        summary, failures = mixed(args, proxy, record, address)
    summary['loss_recoveries'] = record['loss_recoveries'] = proxy.link.loss_recoveries
    summary.update(transport_stalls(proxy))
    record['a16_after'] = a16_snapshot(proxy)
    record['mac_after'] = mac_counters(interface)
    proxy.link.close()
    record['summary'] = summary
    deltas = counter_deltas(record['a16_before'], record['a16_after'])
    deltas['mac'] = mac_deltas(record['mac_before'], record['mac_after'])
    record['deltas'] = deltas
    print(json.dumps({'summary': summary, 'deltas': deltas}, indent=2))
    if failures:
        raise RuntimeError(f"{failures} failure(s) in {mode}: {json.dumps(summary)}")
    if mode == 'replay' and summary['loss_recoveries']:
        raise RuntimeError(f"replay finished only with {summary['loss_recoveries']} loss recoveries")
    return summary


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
    load = sub.add_parser('stress', help='bulk writes with read-back over udp://enN; counts every loss (needs --yes)')
    load.add_argument('--device', required=True, help='udp://enN')
    load.add_argument('--yes', action='store_true')
    load.add_argument('--total', type=lambda v: int(v, 0), default=64 << 20, help='bytes to move (default 64 MiB)')
    load.add_argument('--address', type=lambda v: int(v, 0),
                      help='scratch base; default: the largest free span (it must lie in free memory either way)')
    load.add_argument('--pause', type=float, default=3.0,
                      help='seconds of silence before every third write (0: none)')
    load.add_argument('--retries', type=int, default=0,
                      help='Link loss recovery per transfer; 0 (default) makes any loss a failure')
    mode = load.add_mutually_exclusive_group()
    mode.add_argument('--replay', type=Path, metavar='LAUNCH_JSON',
                      help="write a launch record's uploads (sizes and addresses) back to back, no read-back "
                           'until the end; uploads outside free memory are moved into it')
    mode.add_argument('--nak-hold', type=lambda v: [int(x) for x in v.split(',')], metavar='MS[,MS...]',
                      help='the NAK probe: hold OUT unarmed this long (max 4000) during each 1 MiB write')
    load.add_argument('--replay-count', type=int, default=90, help='uploads to replay (default 90)')
    load.add_argument('--no-verify', dest='verify', action='store_false',
                      help='skip reading the replayed uploads back at the end')
    load.add_argument('--nak-bytes', type=lambda v: int(v, 0), default=1 << 20)
    load.add_argument('--connect-wait', type=float, default=30)
    load.add_argument('--battery-ok', action='store_true',
                      help='the payload cannot read the battery and the operator confirmed AC or >= 50 %%')
    for p in (sub.choices['mac'], watch, a16, armed, load):
        p.add_argument('--record', type=Path, help='write everything observed to this JSON file')
    args = parser.parse_args()
    if args.command == 'arm' and not 4096 <= args.transfer_bytes <= (4 << 20):
        parser.error('--transfer-bytes must be 4 KiB .. 4 MiB')
    if args.command == 'stress' and args.nak_hold and not all(0 < ms <= 4000 for ms in args.nak_hold):
        parser.error('--nak-hold values are 1 .. 4000 ms (under both sides\' 5 s timeouts)')
    if args.command == 'stress' and not 4096 <= args.nak_bytes <= (4 << 20):
        parser.error('--nak-bytes must be 4 KiB .. 4 MiB')

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
        elif args.command == 'stress':
            import q1n1proxy as q
            summary = stress(args, q, record)
            if record['mode'] == 'replay':
                record['result'] = (f"PASS: replay of {summary['uploads']} uploads, {summary['bytes']} bytes in "
                                    f"{summary['seconds']} s ({summary['write_mb_s']} MB/s), no timeout, no "
                                    f"recovery, {summary.get('verified', 'not')} verified")
            elif record['mode'] == 'nak-probe':
                record['result'] = (f"PASS: every hold up to {summary['longest_intact_ms']} ms left the write "
                                    f'intact: the Mac queued, it did not drop')
            else:
                record['result'] = (f"PASS: {summary['bytes']} bytes in {summary['ops']} transfers, no timeout, "
                                    f"write {summary['write_mb_s']} MB/s, read {summary['read_mb_s']} MB/s")
            print(record['result'])
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
        elif args.command == 'stress':
            print('the link itself recovers (a fresh connect works after ~5 s); keep the record and the '
                  'counters above', file=sys.stderr)
        status = 3
    finally:
        record['finished'] = utc()
        if getattr(args, 'record', None):
            args.record.parent.mkdir(parents=True, exist_ok=True)
            args.record.write_text(json.dumps(record, indent=2, default=str) + '\n')
    return status


if __name__ == '__main__':
    sys.exit(main())
