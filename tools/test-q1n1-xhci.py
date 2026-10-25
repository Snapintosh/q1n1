#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build and run the native xHCI/NCM harness with sanitizers.

The A16 is expensive to test against: a wedged device needs a physical replug,
so the driver's logic is exercised here first against a simulated controller.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


# With Q1N1_GADGET_NCM, ncm.c also links the DWC3 device driver for its
# device-role branch. These host-role checks must never reach it.
UNUSED_DWC3_HOOKS = '''#include <stdio.h>
#include <stdlib.h>
#include "qcom-dwc3.h"
static void unused(void) { fprintf(stderr, "DWC3 device driver used by a host-role test\\n"); abort(); }
uint32_t qdwc3_rd(uintptr_t a) { (void)a; unused(); return 0; }
void qdwc3_wr(uintptr_t a, uint32_t v) { (void)a; (void)v; unused(); }
void qdwc3_delay_us(uint64_t us) { (void)us; unused(); }
'''


def main():
    binary = ROOT / 'build' / 'test-q1n1-xhci'
    binary.parent.mkdir(parents=True, exist_ok=True)
    build = ['clang', '-std=gnu11', '-O1', '-g', '-fsanitize=address,undefined',
             '-fno-sanitize-recover=all', '-Wall', '-Wextra', '-Werror',
             f'-I{ROOT}/platform/uefi',
             str(ROOT / 'tools' / 'test-q1n1-xhci.c'),
             str(ROOT / 'platform' / 'uefi' / 'xhci.c'),
             str(ROOT / 'platform' / 'uefi' / 'ncm.c')]
    subprocess.run(build + ['-o', str(binary)], check=True)
    result = subprocess.run([str(binary)])
    if result.returncode:
        return result.returncode
    # The same host path in the opt-in device-role NCM build.
    hooks = ROOT / 'build' / 'test-q1n1-xhci-unused-dwc3.c'
    hooks.write_text(UNUSED_DWC3_HOOKS)
    gadget = binary.with_name('test-q1n1-xhci-gadget')
    subprocess.run(build + ['-DQ1N1_GADGET_NCM', str(ROOT / 'platform' / 'uefi' / 'qcom-dwc3.c'),
                            str(hooks), '-o', str(gadget)], check=True)
    return subprocess.run([str(gadget)]).returncode


if __name__ == '__main__':
    sys.exit(main())
