#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A Q1N1_GADGET_NCM stage must be loadable from the installed default payload.

Before an upload, q1n1proxy.check_stage_transport compares the stage's USB
handoff contract (magic, version and size of struct q1n1_usb_ports) with the
one the running payload published, whenever any port is in the host role. On
this machine that includes an idle port's host attempt, so the check fires or
not depending on when it looks. A gadget stage that grew the shared structs
was refused from the installed payload at generation 0; an earlier one with
the same mismatch had got through on timing.

So, for the default and the gadget build:
1. every image carries the same contract: both stages of each build and both
   q1n1.efi (the installed payload is the default q1n1.efi);
2. the shared structs have the same layout field by field (the contract only
   covers the total size, and a stage adopts a live link in place);
3. the reader's own check, with a port in the host role, accepts the gadget
   stage against the default payload's contract, and refuses one that differs.
"""
from pathlib import Path
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
PORTS_MAGIC = 0x325354524f505355
GADGET = '-DQ1N1_GADGET_NCM'
checks = 0


def check(condition, message):
    global checks
    if not condition:
        raise SystemExit(f'FAIL: {message}')
    checks += 1


def build(out, defines):
    command = ['make', '-f', 'platform/uefi/Makefile', f'OUT={out}', '-j8', 'all']
    if defines:
        command.append(f'Q1N1_DEFINES={defines}')
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f'FAIL: build {out}: {result.stderr[-2000:]}')
    return ROOT / out


def stage_contract(path):
    """The 24 bytes the stage header's contract pointer (offset 40) names."""
    data = path.read_bytes()
    link = struct.unpack_from('<Q', data, 16)[0]
    check(struct.unpack_from('<Q', data, 32)[0] == 0x3154525055533151, f'{path.name}: no USB handoff contract')
    offset = struct.unpack_from('<Q', data, 40)[0] - link
    return data, data[offset:offset + 24]


def efi_contract(path):
    data = path.read_bytes()
    found = []
    at = data.find(struct.pack('<Q', PORTS_MAGIC))
    while at >= 0:
        found.append(data[at:at + 24])
        at = data.find(struct.pack('<Q', PORTS_MAGIC), at + 1)
    check(len(set(found)) == 1, f'{path}: expected one contract constant, found {len(found)}')
    return found[0]


LAYOUT = r'''
#include <stddef.h>
#include <stdio.h>
#include "usb-ports.h"
#define S(t) printf("sizeof %s %zu\n", #t, sizeof(t))
#define O(t, m) printf("%s.%s %zu\n", #t, #m, offsetof(t, m))
int main(void)
{
    S(struct q1n1_usb_ports); S(struct q1n1_usb_port); S(struct q1n1_usb_port_status);
    S(struct qdwc3); S(struct qdwc3_stats); S(struct qdwc3_saved); S(struct xhci); S(struct xhci_device);
    S(struct ncm); S(struct ncm_stats); S(struct ncm_proxy); S(struct ncm_proxy_stats); S(struct q1n1_io);
    O(struct q1n1_usb_ports, status); O(struct q1n1_usb_ports, port); O(struct q1n1_usb_ports, io);
    O(struct q1n1_usb_ports, selected); O(struct q1n1_usb_ports, busy);
    O(struct q1n1_usb_port, device); O(struct q1n1_usb_port, saved); O(struct q1n1_usb_port, host);
    O(struct q1n1_usb_port, ncm); O(struct q1n1_usb_port, proxy); O(struct q1n1_usb_port, base);
    O(struct q1n1_usb_port, device_dma); O(struct q1n1_usb_port, host_dma); O(struct q1n1_usb_port, deadline);
    O(struct q1n1_usb_port, suspended_since); O(struct q1n1_usb_port, qs_hs); O(struct q1n1_usb_port, next_role);
    O(struct q1n1_usb_port, console_ready); O(struct q1n1_usb_port, checksum_state);
    O(struct qdwc3, ep); O(struct qdwc3, pipe); O(struct qdwc3, status_word); O(struct qdwc3, string_buffer);
    O(struct qdwc3, stats); O(struct qdwc3_stats, last_setup);
    O(struct ncm, device); O(struct ncm, mac); O(struct ncm, in_buffer); O(struct ncm, rx_length);
    O(struct ncm, rx_posted); O(struct ncm, stats);
    O(struct ncm_proxy, n); O(struct ncm_proxy, io); O(struct ncm_proxy, ring); O(struct ncm_proxy, frame);
    O(struct ncm_proxy, stats);
    return 0;
}
'''


def layout(defines):
    source = ROOT / 'build' / 'contract-layout.c'
    binary = ROOT / 'build' / ('contract-layout-gadget' if defines else 'contract-layout-default')
    source.write_text(LAYOUT)
    command = ['clang', '-std=gnu11', '-Wall', '-Werror', f'-I{ROOT}/platform/uefi', str(source), '-o', str(binary)]
    if defines:
        command.insert(1, defines)
    subprocess.run(command, check=True)
    return subprocess.run([str(binary)], capture_output=True, text=True, check=True).stdout


def reader_check(stage_image, payload_contract):
    """q1n1proxy's own check, with USB0 in the host role (an idle port's
    attempt is enough to make it compare)."""
    import q1n1proxy as q
    status = struct.pack('<10Q', 0xa600000, 1, 0, 3, 0, 0, 0, 0, 0, 0) + \
        struct.pack('<10Q', 0xa800000, 2, 1, 2, 0, 0, 0, 0, 0, 0)
    proxy = q.Proxy.__new__(q.Proxy)
    proxy.readmem = lambda address, size, live=False: payload_contract if size == 24 else status
    proxy.check_stage_transport(stage_image, {'usb_ports': 0x2000, 'usb_port_count': 2})


def main():
    missing = [tool for tool in ('make', 'clang') if not shutil.which(tool)]
    if missing:
        print(f'SKIP: {", ".join(missing)} not found')
        return 0
    default = build('build/contract-default', '')
    gadget = build('build/contract-gadget', GADGET)
    payload = efi_contract(default / 'q1n1.efi')
    magic, version, size = struct.unpack('<3Q', payload)
    check(magic == PORTS_MAGIC and version >= 1 and size, 'default q1n1.efi contract is malformed')
    check(efi_contract(gadget / 'q1n1.efi') == payload, 'the gadget q1n1.efi publishes a different contract')
    images = {}
    for directory in (default, gadget):
        for name in ('q1n1-stage.bin', 'q1n1-stage-alt.bin'):
            data, contract = stage_contract(directory / name)
            check(contract == payload, f'{directory.name}/{name}: contract {struct.unpack("<3Q", contract)} '
                                       f'differs from the payload\'s {struct.unpack("<3Q", payload)}')
            images[(directory.name, name)] = data
    check(layout('') == layout(GADGET), 'the shared USB structs differ between the default and gadget builds')
    for key, data in images.items():
        reader_check(data, payload)
        checks_before = checks
        try:
            reader_check(data, payload[:16] + struct.pack('<Q', size + 8))
        except Exception as refusal:                       # noqa: BLE001
            check('incompatible USB state layout' in str(refusal), f'{key}: unexpected refusal {refusal}')
        check(checks > checks_before, f'{key}: a different payload contract was not refused')
    print(f'PASS: {checks} USB handoff contract checks (default and Q1N1_GADGET_NCM builds, '
          f'contract version {version}, size {size:#x})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
