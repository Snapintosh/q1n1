#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Datagram loss on the proxy link, and q1n1proxy.Link's recovery from it.

A fake target follows platform/uefi/q1n1-proxy.c for NOP, MEMREAD and
MEMWRITE: it scans for the request magic, checks request and data checksums,
and a read_full that sees no byte for its idle timeout gives the request up
without a reply. The transport carries 1400-byte datagrams like udplink and
drops the ones a test names, in either direction. Time is virtual: a read that
waits advances the clock, and the target notices its idle timeout from it.

What this shows: with nothing lost every transfer is exact; one lost datagram
in a write, a reply, read data or a request header turns into exactly the
timeout seen on hardware; with Q1N1_UDP_RETRIES the transfer is resynchronised
and repeated, and target memory ends up exact. Off by default, nothing changes.
"""
import importlib
import os
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import q1n1proxy as q  # noqa: E402

DATAGRAM = 1400


class Clock:
    now = 0.0


class Target:
    """The request loop of q1n1-proxy.c, for the three requests that matter."""

    def __init__(self, clock, idle):
        self.clock, self.idle = clock, idle
        self.memory = bytearray(1 << 21)
        self.out = bytearray()
        self.state, self.window, self.need, self.last = 'scan', 0, 0, 0.0
        self.timeouts = self.replies = self.xfrerr = 0
        self.csums_off = False

    def reply(self, kind, status, data=b''):
        body = struct.pack('<Ii', kind, status) + data.ljust(24, b'\0')
        self.out += body + struct.pack('<I', q.checksum(body))
        self.replies += 1
        self.xfrerr += status == q.ST_XFRERR

    def tick(self):
        if self.state != 'scan' and self.clock.now - self.last > self.idle:
            self.timeouts += 1                       # read_full gave up: no reply
            self.state, self.window = 'scan', 0

    def feed(self, data):
        self.tick()
        for byte in data:
            self.last = self.clock.now
            if self.state == 'scan':
                self.window = (self.window >> 8) | byte << 24
                if self.window & 0xffffff == 0xAA55FF:
                    self.header = bytearray(struct.pack('<I', self.window))
                    self.state, self.window = 'header', 0
            elif self.state == 'header':
                self.header.append(byte)
                if len(self.header) == 64:
                    self.request()
            else:
                if self.done < self.size:
                    self.memory[self.address + self.done] = byte
                    self.sum = (self.sum * 31337 + (byte ^ 0x5A)) & 0xFFFFFFFF
                else:
                    self.sentinel.append(byte)
                self.done += 1
                if self.done == self.size + (4 if self.csums_off else 0):
                    self.finish_write()

    def request(self):
        h = bytes(self.header)
        kind = struct.unpack_from('<I', h)[0]
        self.state = 'scan'
        if q.checksum(h[:60]) != struct.unpack_from('<I', h, 60)[0]:
            self.reply(kind, -2)
            return
        address, size, expected = struct.unpack_from('<QQI', h, 4)
        if kind == q.REQ_NOP:
            self.csums_off = bool(address & q.FEATURE_DISABLE_DATA_CSUMS)
            self.reply(kind, 0, struct.pack('<Q', address & q.FEATURE_DISABLE_DATA_CSUMS))
        elif kind == q.REQ_MEMREAD:
            data = bytes(self.memory[address:address + size])
            self.reply(kind, 0, struct.pack('<I', q.CHECKSUM_SENTINEL if self.csums_off else q.checksum(data)))
            self.out += data
            if self.csums_off:
                self.out += struct.pack('<I', q.DATA_END_SENTINEL)
        elif kind == q.REQ_MEMWRITE:
            self.address, self.size, self.expected = address, size, expected
            self.done, self.sum, self.sentinel = 0, 0xDEADBEEF, bytearray()
            self.state = 'data'

    def finish_write(self):
        self.state = 'scan'
        total = q.CHECKSUM_SENTINEL if self.csums_off else self.sum ^ 0xADDEDBAD
        ok = total == self.expected and (not self.csums_off or
                                          bytes(self.sentinel) == struct.pack('<I', q.DATA_END_SENTINEL))
        self.reply(q.REQ_MEMWRITE, 0 if ok else q.ST_XFRERR, struct.pack('<I', total))


class LossyDatagrams:
    """udplink.UdpTransport's surface over a dict of datagrams to drop."""

    datagrams = True

    def __init__(self, target, clock, drop_out=(), drop_in=()):
        self.target, self.clock = target, clock
        self.drop_out = drop_out if callable(drop_out) else set(drop_out)
        self.drop_in = set(drop_in)
        self.sent = self.received = 0
        self.buffer = bytearray()

    def write(self, data):
        for at in range(0, len(data), DATAGRAM):
            self.sent += 1
            chunk = data[at:at + DATAGRAM]
            dropped = self.drop_out(chunk) if callable(self.drop_out) else self.sent in self.drop_out
            if not dropped:
                self.target.feed(chunk)

    def pull(self):
        out, self.target.out = bytes(self.target.out), bytearray()
        for at in range(0, len(out), DATAGRAM):
            self.received += 1
            if self.received not in self.drop_in:
                self.buffer += out[at:at + DATAGRAM]

    def read(self, count, timeout=5.0):
        self.pull()
        if len(self.buffer) < count:
            self.clock.now += timeout                # waited the whole timeout
            self.target.tick()
            self.pull()
        if len(self.buffer) < count:
            raise TimeoutError(f'expected {count} bytes, got {len(self.buffer)}')
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def drain(self, quiet, limit):
        self.pull()
        kept = bytes(self.buffer)
        del self.buffer[:]
        self.clock.now += quiet
        self.target.tick()
        return kept

    def close(self):
        pass


def pattern(size, seed):
    """Deterministic, and never the request magic FF 55 AA."""
    return bytes(((k * 7 + seed) % 251) for k in range(size))


class Loss(unittest.TestCase):
    def link(self, idle=5.0, retries=2, **drops):
        clock = Clock()
        target = Target(clock, idle)
        transport = LossyDatagrams(target, clock, **drops)
        link = q.Link(None, timeout=5.0, transport=transport)
        link.loss_retries = retries
        link.target_idle_s = idle
        self.sleep = patch.object(q.time, 'sleep', lambda s: setattr(clock, 'now', clock.now + s))
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        return link, target, transport

    def test_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('Q1N1_UDP_RETRIES', None)
            importlib.reload(q)
            clock = Clock()
            transport = LossyDatagrams(Target(clock, 5.0), clock)
            self.assertEqual(q.Link(None, transport=transport).loss_retries, 0)
            os.environ['Q1N1_UDP_RETRIES'] = '2'
            self.assertEqual(q.Link(None, transport=transport).loss_retries, 2)
            tty_like = type('Tty', (), {'write': None, 'read': None, 'close': None})()
            self.assertEqual(q.Link(None, transport=tty_like).loss_retries, 0)   # streams never retry

    def test_nothing_lost(self):
        link, target, _ = self.link()
        data = pattern(300000, 1)
        link.writemem(0x1000, data)
        self.assertEqual(link.readmem(0x1000, len(data)), data)
        self.assertEqual((link.loss_recoveries, target.timeouts), (0, 0))

    def test_lost_write_datagram_is_the_hardware_timeout(self):
        link, target, transport = self.link(retries=0, drop_out={40})
        with self.assertRaises(TimeoutError):
            link.writemem(0x1000, pattern(1 << 20, 2))
        self.assertEqual(target.timeouts, 1)          # the target gave up waiting, no reply

    def test_lost_write_datagram_recovered(self):
        # The target is still waiting when the host gives up (its idle timeout
        # is longer here), so the filler completes the write and it answers.
        link, target, transport = self.link(idle=8.0, drop_out={40})
        data = pattern(64 << 10, 3)
        link.writemem(0x2000, data)
        self.assertEqual(link.loss_recoveries, 1)
        self.assertEqual(bytes(target.memory[0x2000:0x2000 + len(data)]), data)
        self.assertEqual(link.readmem(0x2000, len(data)), data)

    def test_lost_write_datagram_after_target_gave_up(self):
        link, target, _ = self.link(drop_out={40})
        data = pattern(1 << 20, 4)
        link.writemem(0x0, data)
        self.assertEqual((link.loss_recoveries, target.timeouts), (1, 1))
        self.assertEqual(bytes(target.memory[:len(data)]), data)

    def test_lost_reply(self):
        link, target, transport = self.link()
        link.nop(0)
        transport.drop_in = {transport.received + 1}
        data = pattern(10000, 5)
        link.writemem(0x3000, data)
        self.assertEqual(link.loss_recoveries, 1)
        self.assertEqual(bytes(target.memory[0x3000:0x3000 + len(data)]), data)

    def test_lost_read_data(self):
        link, target, transport = self.link()
        target.memory[0x4000:0x4000 + 200000] = pattern(200000, 6)
        transport.drop_in = {transport.received + 10}
        self.assertEqual(link.readmem(0x4000, 200000), pattern(200000, 6))
        self.assertEqual(link.loss_recoveries, 1)

    def test_lost_request_header(self):
        link, target, transport = self.link()
        transport.drop_out = {transport.sent + 1}
        data = pattern(50000, 7)
        link.writemem(0x5000, data)
        self.assertEqual(link.loss_recoveries, 1)
        self.assertEqual(bytes(target.memory[0x5000:0x5000 + len(data)]), data)

    def test_sentinel_mode_write_recovered(self):
        link, target, transport = self.link(idle=8.0)
        link.nop(q.FEATURE_DISABLE_DATA_CSUMS)
        transport.drop_out = {transport.sent + 3}
        data = pattern(20000, 8)
        link.writemem(0x6000, data)
        self.assertEqual(link.loss_recoveries, 1)
        self.assertTrue(link.features & q.FEATURE_DISABLE_DATA_CSUMS)   # negotiation renewed as it was
        self.assertEqual(bytes(target.memory[0x6000:0x6000 + len(data)]), data)

    def test_retries_are_bounded(self):
        # One data datagram lost in every attempt; the resyncs themselves get through.
        link, target, transport = self.link(retries=2)
        data = pattern(8000, 9)
        transport.drop_out = lambda chunk: chunk == data[1400:2800]
        with self.assertRaises(TimeoutError):
            link.writemem(0x0, data)
        self.assertEqual(link.loss_recoveries, 2)
        # Each resync's filler completed the abandoned write, which the target
        # refused (ST_XFRERR), so nobody had to wait out its idle timeout.
        self.assertEqual((target.xfrerr, target.timeouts), (2, 0))

    def test_a_lost_resync_fails_fast(self):
        link, target, transport = self.link(retries=2)
        transport.drop_out = {transport.sent + k for k in range(1, 400)}  # everything for a while
        with self.assertRaises(TimeoutError):
            link.writemem(0x0, pattern(8000, 9))
        self.assertEqual(link.loss_recoveries, 1)     # its NOP went unanswered: no blind repeats


if __name__ == '__main__':
    unittest.main()
