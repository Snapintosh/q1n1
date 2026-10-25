#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline checks for tools/a16-link-rearm.py: no A16, no ioreg, no ifconfig.

The IORegistry fixtures are shaped like this Mac's `ioreg -a -l` output (the
keys the tool reads), with made-up values. The proxy is a fake over a dict of
memory, so the uploads, read-backs and restores are checked byte for byte.
"""
import importlib.util
import json
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('rearm', TOOLS / 'a16-link-rearm.py')
rearm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rearm)


def port(number, role, draws=False, connected=True):
    return {'IORegistryEntryName': 'Port-USB-C', 'PortNumber': number, 'ConnectionActive': connected,
            'IOAccessoryUSBConnectString': 'Device', 'ConnectionCount': 7,
            'IOAccessoryPowerCurrentLimits': [3000, 0, 0, 0, 0] if draws else [0, 0, 0, 0, 0],
            'IORegistryEntryChildren': [{'IORegistryEntryName': 'USB2', 'DataRoleDescription': role}]}


def a16_device(serial, ncm, en=None):
    interfaces = [{'bInterfaceNumber': 0, 'bInterfaceClass': 2, 'bInterfaceSubClass': 2},
                  {'bInterfaceNumber': 1, 'bInterfaceClass': 10, 'bInterfaceSubClass': 0}]
    if ncm:
        interfaces += [{'bInterfaceNumber': 2, 'bInterfaceClass': 2, 'bInterfaceSubClass': 13,
                        'IORegistryEntryChildren': [{'IORegistryEntryChildren': [{'BSD Name': en}]}]},
                       {'bInterfaceNumber': 3, 'bInterfaceClass': 10, 'bInterfaceSubClass': 0}]
    for interface in interfaces:                 # as on the Mac: the nubs repeat the serial
        interface.update(IOObjectClass='IOUSBHostInterface', kUSBSerialNumberString=serial)
    return {'IOObjectClass': 'IOUSBHostDevice', 'kUSBSerialNumberString': serial,
            'USB Product Name': 'q1n1 A16 EL2 serial', 'Device Speed': 2,
            'locationID': 0x2100000, 'sessionID': 1234, 'IORegistryEntryChildren': interfaces}


class FakeRun:
    """subprocess.run for ioreg and ifconfig, from fixtures."""

    def __init__(self, classes, interfaces):
        self.classes, self.interfaces = classes, interfaces

    def __call__(self, argv, **kwargs):
        if argv[0] == 'ioreg':
            return types.SimpleNamespace(stdout=plistlib.dumps(self.classes.get(argv[-1], [])))
        text = self.interfaces.get(argv[1], '')
        return types.SimpleNamespace(stdout=text if kwargs.get('text') else text.encode())


ACTIVE = 'status: active\n\tinet6 fe80::1%en16 prefixlen 64\n'


class Fixtures(unittest.TestCase):
    def snapshot(self, classes, interfaces):
        run = FakeRun(classes, interfaces)
        with patch.object(rearm.udplink.subprocess, 'run', run):
            return rearm.mac_snapshot(run)

    def test_after_reset_the_mac_is_the_host(self):
        # What this Mac showed on 2026-10-24 after a reset: EL2B on its own
        # xHCI, port 3 in the host role, the device-mode NCM interfaces idle.
        snap = self.snapshot({'AppleHPMInterfaceType10': [port(3, 'Host')],
                              'IOUSBHostDevice': [a16_device('A16-Q1N1-EL2B', ncm=False)],
                              'AppleUSBDeviceNCM11Data': [{'BSD Name': 'en6'}]},
                             {'en6': 'status: inactive\n'})
        kinds = [k for k, _ in rearm.classify(snap)]
        self.assertEqual(kinds, ['mac-is-host'])
        self.assertEqual(snap['ports'][0]['data_roles'], {'USB2': 'Host'})
        self.assertFalse(snap['ports'][0]['mac_draws_power'])
        self.assertEqual(snap['a16_devices'][0]['speed'], 'high')
        self.assertEqual(len(snap['a16_devices']), 1)         # not its interface nubs again
        self.assertEqual(snap['ports'][0]['name'], 'Port-USB-C@3')

    def test_gadget_link_is_found_and_preferred(self):
        snap = self.snapshot({'IOUSBHostDevice': [{'IORegistryEntryChildren': [
                                  a16_device('A16-Q1N1-EL2B', ncm=True, en='en16')]}],
                              'AppleUSBDeviceNCM11Data': [{'BSD Name': 'en6'}]},
                             {'en6': 'status: inactive\n', 'en16': ACTIVE})
        kinds = [k for k, _ in rearm.classify(snap)]
        self.assertEqual(kinds, ['gadget-ncm', 'mac-is-host'])
        self.assertEqual(snap['gadget_ncm'], {'en16': {'present': True, 'active': True, 'link_local': True}})
        self.assertTrue(snap['a16_devices'][0]['ncm_function'])

    def test_replugged_host_link_and_nothing(self):
        snap = self.snapshot({'AppleHPMInterfaceType10': [port(3, 'Device')],
                              'AppleUSBDeviceNCM11Data': [{'BSD Name': 'en6'}]}, {'en6': ACTIVE})
        self.assertEqual([k for k, _ in rearm.classify(snap)], ['ncm-host'])
        empty = self.snapshot({}, {})
        self.assertEqual([k for k, _ in rearm.classify(empty)], ['none'])

    def test_a_phone_with_ncm_is_not_the_a16(self):
        phone = a16_device('PHONE', ncm=True, en='en9')
        snap = self.snapshot({'IOUSBHostDevice': [phone]}, {'en9': ACTIVE})
        self.assertEqual(snap['a16_devices'], [])
        self.assertEqual(snap['gadget_ncm'], {})

    def test_watch_reports_only_changes(self):
        before = {'ports': {'p3': {'roles': {'USB2': 'Device'}}}, 'ncm': {'en6': True}}
        after = {'ports': {'p3': {'roles': {'USB2': 'Host'}}}, 'ncm': {'en6': False, 'en16': True}}
        self.assertEqual(rearm.changes(before, after),
                         ['ncm.en16: None -> True', 'ncm.en6: True -> False', 'ports.p3.roles.USB2: Device -> Host'])
        self.assertEqual(rearm.changes(after, after), [])

    def test_other_clients(self):
        ps = ('  10     1 /usr/bin/python3 tools/q1n1proxy.py --device udp://en6 info\n'
              '  11     1 /bin/zsh\n  12    11 python3 tools/a16-link-rearm.py arm x --yes\n'
              '  13     1 python3 tools/cycle.py installed-fresh --boot\n')
        self.assertEqual([c.split(':')[0] for c in rearm.other_clients(ps, {11, 12})], ['10', '13'])
        self.assertEqual(rearm.other_clients(ps, {10, 11, 12, 13}), [])


class Memory:
    """A fake q1n1 proxy: bootinfo, port records and memory."""

    P_VECTOR = 0x00B

    def __init__(self, endpoint='/dev/cu.usbmodemA16_Q1N1_EL2B1', generation=0, role=2, features=0):
        self.endpoint, self.memory, self.reads, self.vectors = endpoint, {}, [], []
        self.info = {'stage_generation': generation, 'current_el': 2, 'image_base': 0xbde23000,
                     'return_armed': 1, 'usb_ports': 0x1000, 'usb_port_count': 2, 'heap_base': 0x9000000,
                     'stage_base': 0xb0000000, 'stage_size': 16 << 20, 'address': 0x5000}
        self.features = features
        error = (-3) & ((1 << 64) - 1)
        self.write(0x1000, struct.pack('<10Q', 0xa600000, 1, 0, 9, 0, error, 0, 0x3000, 0x3100, 0x3200))
        self.write(0x1050, struct.pack('<10Q', 0xa800000, role, 1, 2, 0, 0, 0x2000, 0, 0x2400, 0x2500))
        device = struct.pack('<14Q8I', 50, 40, 10, 30, 1, 2, 3, 4, 5000, 6000, 0, 0, 0, 0, 0, 7, 1, 0, 0, 0, 8, 0)
        self.write(0x2000, device + struct.pack('<8Q', 6, 2, 11, 12, 0, 1, 1, 8192))
        self.write(0x2400, struct.pack('<12QI', 11, 12, 13, 14, 15, 16, 0, 0, 0, 0, 0, 0, 9))
        self.write(0x2500, struct.pack('<9Q', 1, 2, 3, 4, 0, 0, 5, 6, 0))
        self.write(0x3100, struct.pack('<12QI', *([0] * 12), 4))
        self.write(0x3200, struct.pack('<9Q', *([0] * 9)))

    def write(self, address, data):
        for k, b in enumerate(data):
            self.memory[address + k] = b

    def bootinfo(self):
        return dict(self.info)

    def stage_features(self):
        return self.features

    def readmem(self, address, size, live=False):
        self.reads.append(size)
        return bytes(self.memory.get(address + k, 0) for k in range(size))

    def writemem(self, address, data):
        self.write(address, data)

    def flush_code(self, address, size):
        pass

    def request(self, opcode, *args):
        self.vectors.append((opcode, args))

    def read32(self, address):
        return {0xa600000: 0x20, 0xa600420: 0x603, 0xa600430: 0x2a0}.get(address, 0)


class TargetSide(unittest.TestCase):
    def test_a16_snapshot_names_the_wedge_and_reads_gadget_counters(self):
        proxy = Memory(generation=1, features=3)
        snap = rearm.a16_snapshot(proxy, portsc=True)
        host, device = snap['ports']
        self.assertEqual((host['role'], host['last_error']), ('host', -3))
        self.assertIn('Full-speed wedge', host['last_error_meaning'])
        self.assertEqual(host['portsc'], [0x603, 0x2a0])
        self.assertEqual(host['portsc_speed'], ['full', 'none'])
        self.assertEqual(device['device']['setups'], 30)
        self.assertEqual(device['device']['resets'], 2)
        self.assertEqual(device['device']['speed'], 'high')
        self.assertEqual(device['gadget']['alt'], 1)
        self.assertEqual(device['gadget']['tx_limit'], 8192)
        self.assertEqual(device['ncm']['frames_in'], 13)
        self.assertEqual(device['udp']['solicitations'], 5)
        self.assertTrue(snap['gadget_ncm_stage'])

    def test_counters_found_through_the_table(self):
        # Stage feature bit 3: the counters are in qdwc3_ncm_table, named at
        # image offset 64, and the device stats are the plain 144 bytes.
        proxy = Memory(generation=1, features=11)
        proxy.write(proxy.info['image_base'] + 64, struct.pack('<QQ', rearm.NCM_TABLE_MAGIC, 0x6000))
        proxy.write(0x6000, struct.pack('<3Q4Q', rearm.NCM_TABLE_MAGIC, 1, 2, 0xa600000, 0, 0xa800000, 0x7000))
        proxy.write(0x7000, struct.pack('<16Q', 7, 2, 30, 31, 0, 1, 1, 16384, 5, 0, 0, 3, 40, 900, 0, 1))
        proxy.write(0x2000 + 144, bytes(128))               # nothing appended to read by mistake
        device = rearm.a16_snapshot(proxy)['ports'][1]
        self.assertEqual(device['gadget']['tx_limit'], 16384)
        self.assertEqual((device['gadget']['service_gap_max_us'], device['gadget']['probes_deferred']), (900, 1))
        self.assertEqual(device['device']['setups'], 30)

    def test_table_version_2_adds_pool_wait_and_loop_counters(self):
        proxy = Memory(generation=1, features=11)
        proxy.write(proxy.info['image_base'] + 64, struct.pack('<QQ', rearm.NCM_TABLE_MAGIC, 0x6000))
        proxy.write(0x6000, struct.pack('<3Q4QQ', rearm.NCM_TABLE_MAGIC, 2, 2, 0xa600000, 0, 0xa800000, 0x7000,
                                        0x7800))
        proxy.write(0x7000, struct.pack('<22Q', 7, 2, 30, 31, 0, 1, 1, 16384, 5, 0, 0, 3, 40, 900, 0, 1,
                                        12, 0, 250000, 2, 0, 1))
        proxy.write(0x7800, struct.pack('<7Q', 4, 180000, 55, 300, 51000, 0, 300100))
        snap = rearm.a16_snapshot(proxy)
        gadget = snap['ports'][1]['gadget']
        self.assertEqual((gadget['rx_queue_max'], gadget['host_wait_max_us'], gadget['host_waits_100ms'],
                          gadget['holds']), (12, 250000, 2, 1))
        self.assertEqual(snap['ncm_table'], {'address': 0x6000, 'version': 2, 'loop': 0x7800})
        self.assertEqual((snap['loop']['panel_max_us'], snap['loop']['panels_deferred'], snap['loop']['hold_last_us']),
                         (180000, 55, 300100))
        # A version-1 table is read as before: 16 counters, no loop.
        proxy.write(0x6000, struct.pack('<3Q', rearm.NCM_TABLE_MAGIC, 1, 2))
        snap = rearm.a16_snapshot(proxy)
        self.assertEqual(len(snap['ports'][1]['gadget']), 16)
        self.assertNotIn('loop', snap)

    def test_deltas_keep_peaks_and_difference_counts(self):
        before = {'proxy': {'timeouts': 1}, 'ports': [{'gadget': {'blocks_in': 10, 'rx_queue_max': 3,
                                                                   'host_wait_max_us': 5, 'alt': 1}}],
                  'loop': {'panels_deferred': 2, 'panel_max_us': 9, 'hold_request_ms': 0}}
        after = {'proxy': {'timeouts': 1}, 'ports': [{'gadget': {'blocks_in': 25, 'rx_queue_max': 9,
                                                                  'host_wait_max_us': 7, 'alt': 1}}],
                 'loop': {'panels_deferred': 12, 'panel_max_us': 9, 'hold_request_ms': 0}}
        deltas = rearm.counter_deltas(before, after)
        self.assertEqual(deltas['proxy'], {'timeouts': 0})
        self.assertEqual(deltas['usb0.gadget'], {'blocks_in': 15, 'rx_queue_max': 9, 'host_wait_max_us': 7})
        self.assertEqual(deltas['loop'], {'panels_deferred': 10, 'panel_max_us': 9})

    def test_mac_queue_summary_and_ip6_drops_are_parsed(self):
        outputs = {
            'netstat -I en9 -b -d': 'Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll Drop\n'
                                    'en9 1500 <Link#9> 0:0:0:0:0:0 100 0 999 50 1 888 0 2\n',
            'netstat -s -p udp': '\t\t7 dropped due to full socket buffers\n',
            'netstat -s -p ip6': '\t\t3 output packets dropped due to no bufs, etc.\n',
            'netstat -qq -I en9': 'en9:\n     [ sched:           FQ_CODEL  qlength:    4/128 ]\n'
                                  '     [ dequeued pkts:  9000  bytes: 1 dropped pkts:   6 bytes: 9000 ]\n'}
        run = lambda command, **kw: types.SimpleNamespace(stdout=outputs[' '.join(command)])  # noqa: E731
        record = rearm.mac_counters('en9', run=run)
        self.assertEqual((record['opkts'], record['oerrs'], record['drop']), (50, 1, 2))
        self.assertEqual((record['udp_full_socket_drops'], record['ip6_output_no_bufs']), (7, 3))
        self.assertEqual((record['queue_length'], record['queue_limit'], record['queue_dropped']), (4, 128, 6))
        self.assertEqual(rearm.mac_deltas(record, dict(record, queue_dropped=9, oerrs=1)),
                         {'ierrs': 0, 'oerrs': 0, 'drop': 0, 'udp_full_socket_drops': 0, 'ip6_output_no_bufs': 0,
                          'queue_dropped': 3})

    def test_default_stage_has_no_gadget_counters(self):
        snap = rearm.a16_snapshot(Memory(generation=1, features=1))
        device = snap['ports'][1]
        self.assertNotIn('gadget', device)
        self.assertNotIn('ncm', device)
        self.assertNotIn('portsc', snap['ports'][0])

    def test_upload_reads_back_below_the_cdc_quantum(self):
        proxy = Memory()
        image = bytes(range(256)) * 40
        rearm.verified_upload(proxy, 0xb0800000, image, tty=True)
        self.assertTrue(proxy.reads and max(proxy.reads) <= rearm.CDC_READ_CHUNK)
        self.assertEqual(proxy.vectors, [(proxy.P_VECTOR, (0xb0800000, 0x5000))])
        proxy = Memory(endpoint='udp://en16')
        rearm.verified_upload(proxy, 0xb0800000, image, tty=False)
        self.assertEqual(proxy.reads, [len(image)])

    def test_upload_that_does_not_verify_never_jumps(self):
        proxy = Memory()
        proxy.writemem = lambda address, data: proxy.write(address, data[:-1] + b'\xff')
        with self.assertRaises(RuntimeError):
            rearm.verified_upload(proxy, 0xb0800000, bytes(4000), tty=True)
        self.assertEqual(proxy.vectors, [])

    def test_round_trip_puts_the_original_back(self):
        proxy = Memory()
        original = bytes((7 * k) & 0xff for k in range(8192))
        proxy.write(0x9000000, original)
        result = rearm.round_trip(proxy, 0x9000000, 8192)
        self.assertTrue(result['match'] and result['restored'])
        self.assertEqual(proxy.readmem(0x9000000, 8192), original)

    def test_stage_feature_bit(self):
        header = bytearray(64)
        struct.pack_into('<QQ', header, 48, rearm.STAGE_FEATURES_MAGIC, 3)
        self.assertTrue(rearm.stage_has_gadget_ncm(bytes(header)))
        struct.pack_into('<QQ', header, 48, rearm.STAGE_FEATURES_MAGIC, 1)
        self.assertFalse(rearm.stage_has_gadget_ncm(bytes(header)))
        self.assertFalse(rearm.stage_has_gadget_ncm(b'short'))

    def test_built_stages_carry_the_right_bit(self):
        root = TOOLS.parent / 'build'
        pairs = [(root / 'uefi-gadget-ncm' / 'q1n1-stage.bin', True), (root / 'uefi' / 'q1n1-stage.bin', False)]
        present = [(p, want) for p, want in pairs if p.exists()]
        if not present:
            self.skipTest('no stage built (make -f platform/uefi/Makefile [OUT=... Q1N1_DEFINES=...])')
        for path, want in present:
            self.assertEqual(rearm.stage_has_gadget_ncm(path.read_bytes()), want, path)


class Refusals(unittest.TestCase):
    """arm refuses before touching the target whenever a precondition fails."""

    def args(self, **kw):
        base = dict(yes=True, stage='stage.bin', device=None, wait=1, connect_wait=0, transfer_bytes=4096,
                    battery_ok=False)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def run_arm(self, args, proxy, image, clients=''):
        q = types.SimpleNamespace(connect=lambda device, wait=0: proxy)
        with patch.object(rearm, 'ancestors', return_value=(clients, set())), \
             patch.object(rearm, 'mac_snapshot', return_value={}), \
             patch.object(rearm.Path, 'read_bytes', return_value=image):
            return rearm.arm(args, q, {})

    def gadget_image(self):
        image = bytearray(4096)
        struct.pack_into('<QQ', image, 48, rearm.STAGE_FEATURES_MAGIC, 3)
        return bytes(image)

    def test_needs_yes(self):
        with self.assertRaisesRegex(rearm.Refused, '--yes'):
            self.run_arm(self.args(yes=False), Memory(), self.gadget_image())

    def test_other_client(self):
        with self.assertRaisesRegex(rearm.Refused, 'another hardware client'):
            self.run_arm(self.args(), Memory(), self.gadget_image(),
                         clients='  44 1 python3 tools/a16gpu.py --device udp://en6 status\n')

    def test_not_a_gadget_stage(self):
        with self.assertRaisesRegex(rearm.Refused, 'not a Q1N1_GADGET_NCM stage'):
            self.run_arm(self.args(), Memory(), bytes(4096))

    def test_live_host_link_is_left_alone(self):
        proxy = Memory()
        proxy.write(0x1000 + 16, struct.pack('<Q', 1))          # USB0 host, connected
        with self.assertRaisesRegex(rearm.Refused, 'host-role NCM link is up'):
            self.run_arm(self.args(), proxy, self.gadget_image())

    def test_a_different_handoff_layout_is_refused_whatever_the_roles(self):
        proxy = Memory()
        proxy.write(0x1000 + 16, struct.pack('<Q', 0))      # nothing in the host role
        payload = struct.pack('<3Q', 0x325354524f505355, 1, 0x6150)
        proxy.write(0x1000 - 24, payload)
        proxy.live_battery = lambda: (0, {'plausible': True, 'ac': True, 'percent': 90})
        image = bytearray(self.gadget_image())
        struct.pack_into('<QQQQ', image, 16, 0xb0800000, 0, 0x3154525055533151, 0xb0800000 + 0x800)
        image[0x800:0x818] = struct.pack('<3Q', 0x325354524f505355, 2, 0x6320)
        proxy.pick_stage = lambda path: (path, bytes(image), 0xb0800000)
        with self.assertRaisesRegex(rearm.Refused, 'changes the USB handoff layout'):
            self.run_arm(self.args(), proxy, bytes(image))
        self.assertEqual(rearm.stage_contract(bytes(image)), struct.pack('<3Q', 0x325354524f505355, 2, 0x6320))
        image[0x800:0x818] = payload
        proxy.check_stage_transport = lambda data, info: None
        proxy.request = lambda *a: None
        proxy.link = types.SimpleNamespace(close=lambda: None)
        with patch.object(rearm, 'verified_upload', side_effect=RuntimeError('uploaded')):
            with self.assertRaisesRegex(RuntimeError, 'uploaded'):   # the same layout goes ahead
                self.run_arm(self.args(), proxy, bytes(image))

    def test_battery(self):
        proxy = Memory()
        proxy.write(0x1000 + 16, struct.pack('<Q', 0))
        proxy.live_battery = lambda: (0, {'plausible': True, 'ac': False, 'percent': 31, 'text': '31%'})
        with self.assertRaisesRegex(rearm.Refused, 'battery check failed: 31%'):
            self.run_arm(self.args(), proxy, self.gadget_image())
        proxy.live_battery = lambda: (_ for _ in ()).throw(RuntimeError('no live battery path'))
        with self.assertRaisesRegex(rearm.Refused, 'battery unreadable'):
            self.run_arm(self.args(), proxy, self.gadget_image())


class BulkTarget:
    """A fake proxy over one bytearray of 'free' DRAM, for the stress loop.
    With a NAK-probe control it also models the Mac: a hold longer than
    `drops_after_ms` loses the write that follows."""

    BASE = 0x880000000
    LOOP = 0x7800

    def __init__(self, fail_at=None, ram=48 << 20, table=False, drops_after_ms=None):
        self.ram = bytearray(ram)
        self.calls, self.fail_at = 0, fail_at
        self.trace = []
        self.table, self.drops_after_ms, self.hold = table, drops_after_ms, 0
        self.loop = bytearray(56)
        self.endpoint = 'udp://en99'
        entry = struct.pack('<IIQQQQ8x', 7, 0, self.BASE, 0, len(self.ram) // 4096, 0)   # stride 48
        heap = struct.pack('<IIQQQQ8x', 2, 0, 0x9000000, 0, 0x4000, 0)
        self.map = entry + heap
        self.info = {'current_el': 2, 'stage_generation': 1, 'image_base': 0xb0000000, 'return_armed': 1,
                     'usb_ports': 0, 'proxy_stats': 0, 'memory_map': 0x1000, 'memory_map_size': len(self.map),
                     'memory_map_stride': 48, 'heap_base': 0x9000000, 'heap_size': 0x4000000,
                     'stage_base': 0xb0000000, 'stage_size': 16 << 20}
        self.link = types.SimpleNamespace(loss_retries=0, loss_recoveries=0, resyncs=0, close=lambda: None,
                                          transport=types.SimpleNamespace(send_stalls=2, send_stall_max_s=0.0042))
        self.link.resync = lambda pending: setattr(self.link, 'resyncs', self.link.resyncs + 1)

    def bootinfo(self):
        return dict(self.info)

    def stage_features(self):
        return 11 if self.table else 7

    def write_chunk(self):
        return 1 << 20

    def live_battery(self):
        return 0, {'plausible': True, 'ac': True, 'percent': 80, 'text': 'AC'}

    def readmem(self, address, size, live=False):
        if address == 0x1000:
            return self.map
        if self.table and address == self.info['image_base'] + 64:
            return struct.pack('<QQ', rearm.NCM_TABLE_MAGIC, 0x6000)
        if self.table and address == 0x6000:
            return struct.pack('<3Q', rearm.NCM_TABLE_MAGIC, 2, 2)[:size]
        if self.table and address == 0x6000 + 24:
            return bytes(size)
        if self.table and address == 0x6000 + 24 + 32:
            return struct.pack('<Q', self.LOOP)
        if self.table and address == self.LOOP:
            return bytes(self.loop[:size])
        self.trace.append(('read', address, size))
        at = address - self.BASE
        return bytes(self.ram[at:at + size])

    def writemem(self, address, data):
        if self.table and address == self.LOOP + rearm.LOOP_HOLD_REQUEST:
            self.hold = struct.unpack('<Q', data)[0]
            self.loop[48:56] = struct.pack('<Q', self.hold * 1000)      # the stage's hold_last_us
            return
        self.calls += 1
        self.trace.append(('write', address, len(data)))
        if self.calls == self.fail_at:
            raise TimeoutError('expected 1 bytes, got 0')
        hold, self.hold = self.hold, 0
        if self.drops_after_ms is not None and hold > self.drops_after_ms:
            raise TimeoutError('no reply to 0x3aa55ff')
        at = address - self.BASE
        if not 0 <= at <= len(self.ram) - len(data):
            raise AssertionError(f'write outside the free memory: {address:#x}+{len(data):#x}')
        self.ram[at:at + len(data)] = data


class Stress(unittest.TestCase):
    def test_free_spans_leave_q1n1_alone(self):
        info = {'heap_base': 0x2000_0000, 'heap_size': 0x100_0000, 'stage_base': 0x2800_0000, 'stage_size': 0x100_0000}
        memory_map = struct.pack('<IIQQQQ8x', 7, 0, 0x2000_0000, 0, 0x10000, 0)   # 256 MiB of free memory
        spans = rearm.free_spans(memory_map, 48, info)
        self.assertEqual(spans, [(0x2100_0000, 0x2800_0000), (0x2900_0000, 0x3000_0000)])
        with self.assertRaises(ValueError):
            rearm.free_spans(memory_map[:40], 48, info)

    def test_window_must_be_free(self):
        target = BulkTarget()
        self.assertEqual(rearm.stress_window(target, target.info, 16 << 20), BulkTarget.BASE)
        with self.assertRaisesRegex(rearm.Refused, 'not inside firmware-reported free memory'):
            rearm.stress_window(target, target.info, 16 << 20, 0x9000000)       # the q1n1 heap

    def args(self, **kw):
        base = dict(yes=True, device='udp://en99', total=40 << 20, address=None, pause=0, retries=0,
                    connect_wait=0, battery_ok=False, replay=None, replay_count=90, verify=True,
                    nak_hold=None, nak_bytes=1 << 20)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def run_stress(self, target, **kw):
        q = types.SimpleNamespace(connect=lambda device, wait=0: target)
        with patch.object(rearm, 'ancestors', return_value=('', set())), \
             patch.object(rearm, 'mac_counters', return_value={'drop': 0}):
            record = {}
            try:
                return rearm.stress(self.args(**kw), q, record), record
            except RuntimeError as problem:
                return problem, record

    def test_clean_run_passes(self):
        summary, record = self.run_stress(BulkTarget())
        self.assertEqual((summary['failures'], summary['timeouts']), (0, 0))
        self.assertGreaterEqual(summary['bytes'], 40 << 20)
        self.assertIn(13942784, [op['size'] for op in record['ops']])

    def test_a_timeout_fails_the_run_and_is_resynchronised(self):
        target = BulkTarget(fail_at=2)
        problem, record = self.run_stress(target)
        self.assertIsInstance(problem, RuntimeError)
        self.assertEqual(record['summary']['timeouts'], 1)
        self.assertEqual(target.link.resyncs, 1)
        self.assertTrue(all(op.get('match') for op in record['ops'] if 'error' not in op))

    # ---- boot replay ----

    def launch(self, uploads):
        path = Path(self.scratch.name) / 'launch.json'
        path.write_text(json.dumps({'uploads': [{'address': hex(a), 'bytes': n, 'sha256': '00',
                                                 'transport_acknowledged': True} for a, n in uploads]}))
        return path

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.scratch.cleanup()

    def test_replay_plan_keeps_free_addresses_and_moves_the_rest_clear_of_them(self):
        spans = [(0x1000_0000, 0x1100_0000)]
        uploads = [(0x9000000, 0x3000), (0x1000_0000, 0x2000), (0x1000_1000, 64), (0xb0600000, 5000),
                   (0x1080_0000, 0x10)]
        planned, kept, base = rearm.plan_replay(uploads, spans)
        self.assertEqual(kept, 3)
        self.assertEqual(planned[1:3] + planned[4:], [(0x1000_0000, 0x2000), (0x1000_1000, 64), (0x1080_0000, 0x10)])
        self.assertEqual(base, 0x10a0_0000)                       # in the larger gap, on a 2 MiB step
        self.assertEqual(planned[0], (base, 0x3000))
        self.assertEqual(planned[3], (base + 0x3000, 5000))       # page-aligned, in order
        for a, n in (planned[0], planned[3]):
            self.assertTrue(all(a + n <= x or x + m <= a for x, m in (planned[1], planned[2], planned[4])))
        with self.assertRaisesRegex(rearm.Refused, 'no free stretch'):
            rearm.plan_replay([(0x9000000, 0x200_0000)], spans)
        self.assertEqual(rearm.last_writers([(0, 100), (50, 10), (200, 8), (0, 8)]), [1, 2, 3])

    def test_launch_uploads_load_first_n(self):
        path = self.launch([(0x8c0088000, 13942784), (0x8c0dd4000, 12828672), (0x8c1a10000, 475136)])
        self.assertEqual(rearm.load_uploads(path, 2), [(0x8c0088000, 13942784), (0x8c0dd4000, 12828672)])
        path.write_text(json.dumps({'uploads': []}))
        with self.assertRaisesRegex(rearm.Refused, 'lists no uploads'):
            rearm.load_uploads(path, 90)

    def test_replay_writes_back_to_back_then_verifies(self):
        target = BulkTarget()
        sizes = [459904, 64, 64, 1049352, 3145728, 9312, 13942784, 12828672, 475136, 4096]
        uploads, at = [], BulkTarget.BASE
        for n in sizes:
            uploads.append((at, n))
            at += rearm.page_up(n)
        uploads[1] = (0xaf0300000, 64)                            # outside free memory: moved
        uploads[2] = (uploads[0][0] + 4096, 64)                   # overwrites part of the first
        summary, record = self.run_stress(target, replay=self.launch(uploads), replay_count=9)
        self.assertEqual((summary['uploads'], summary['failures'], summary['loss_recoveries']), (9, 0, 0))
        self.assertEqual(record['replay']['kept_addresses'], 8)
        writes = [i for i, t in enumerate(target.trace) if t[0] == 'write']
        reads = [i for i, t in enumerate(target.trace) if t[0] == 'read']
        self.assertEqual(len(writes), 9)
        self.assertTrue(max(writes) < min(reads))                 # no read-back until every upload is in
        self.assertEqual(summary['verified'], 8)                  # the first upload was partly overwritten
        self.assertEqual(summary['mismatched'], [])
        self.assertEqual((summary['send_stalls'], record['mode']), (2, 'replay'))

    def test_replay_stops_at_the_first_lost_upload(self):
        target = BulkTarget(fail_at=4)
        path = self.launch([(BulkTarget.BASE + k * (1 << 20), 1 << 20) for k in range(8)])
        problem, record = self.run_stress(target, replay=path)
        self.assertIsInstance(problem, RuntimeError)
        self.assertEqual(len(record['ops']), 4)
        self.assertIn('TimeoutError', record['ops'][-1]['error'])
        self.assertNotIn('verified', record['summary'])

    def test_replay_that_needed_a_recovery_does_not_pass(self):
        target = BulkTarget()
        target.link.loss_recoveries = 1
        problem, record = self.run_stress(target, replay=self.launch([(BulkTarget.BASE, 4096)]))
        self.assertIsInstance(problem, RuntimeError)
        self.assertIn('loss recoveries', str(problem))

    # ---- the NAK probe ----

    def test_nak_probe_holds_then_reads_back(self):
        target = BulkTarget(table=True)
        summary, record = self.run_stress(target, nak_hold=[200, 1000, 3000])
        self.assertEqual((summary['failures'], summary['longest_intact_ms']), (0, 3000))
        self.assertEqual([r['hold_last_us'] for r in record['nak_probe']], [200000, 1000000, 3000000])
        self.assertTrue(all(r['match'] for r in record['nak_probe']))

    def test_nak_probe_reports_where_the_mac_starts_dropping(self):
        target = BulkTarget(table=True, drops_after_ms=2000)
        problem, record = self.run_stress(target, nak_hold=[200, 1000, 3000])
        self.assertIsInstance(problem, RuntimeError)
        self.assertEqual([r.get('match') for r in record['nak_probe']], [True, True, None])
        self.assertEqual(record['summary']['longest_intact_ms'], 1000)
        self.assertEqual(target.link.resyncs, 1)

    def test_nak_probe_needs_a_version_2_stage(self):
        problem, record = self.run_stress(BulkTarget(), nak_hold=[200])
        self.assertIsInstance(problem, rearm.Refused)
        self.assertIn('NAK-probe control', str(problem))
        self.assertNotIn('nak_probe', record)

    def test_refusals(self):
        with self.assertRaisesRegex(rearm.Refused, '--yes'):
            rearm.stress(self.args(yes=False), None, {})
        with self.assertRaisesRegex(rearm.Refused, 'udp://'):
            rearm.stress(self.args(device='/dev/cu.usbmodemX'), None, {})


if __name__ == '__main__':
    sys.exit(unittest.main())
