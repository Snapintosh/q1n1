#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline checks for tools/a16-link-rearm.py: no A16, no ioreg, no ifconfig.

The IORegistry fixtures are shaped like this Mac's `ioreg -a -l` output (the
keys the tool reads), with made-up values. The proxy is a fake over a dict of
memory, so the uploads, read-backs and restores are checked byte for byte.
"""
import importlib.util
from pathlib import Path
import plistlib
import struct
import sys
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

    def test_battery(self):
        proxy = Memory()
        proxy.write(0x1000 + 16, struct.pack('<Q', 0))
        proxy.live_battery = lambda: (0, {'plausible': True, 'ac': False, 'percent': 31, 'text': '31%'})
        with self.assertRaisesRegex(rearm.Refused, 'battery check failed: 31%'):
            self.run_arm(self.args(), proxy, self.gadget_image())
        proxy.live_battery = lambda: (_ for _ in ()).throw(RuntimeError('no live battery path'))
        with self.assertRaisesRegex(rearm.Refused, 'battery unreadable'):
            self.run_arm(self.args(), proxy, self.gadget_image())


if __name__ == '__main__':
    sys.exit(unittest.main())
