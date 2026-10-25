#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the port lifecycle, device halt, DWC3 device and reader discovery
regressions, in the default build and with Q1N1_GADGET_NCM."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    flags = ['clang', '-std=gnu11', '-O1', '-g', '-fsanitize=address,undefined',
             '-fno-sanitize-recover=all', '-Wall', '-Wextra', '-Werror',
             '-Iplatform/uefi']
    gadget = ['-DQ1N1_GADGET_NCM']   # the opt-in device-role CDC-NCM build (A16-USB-REARM.md)
    for name, sources, defines in (
        ('test-q1n1-usb-ports', ['tools/test-q1n1-usb-ports.c', 'platform/uefi/usb-ports.c'], []),
        ('test-qdwc3-stop', ['tools/test-qdwc3-stop.c'], []),
        ('test-q1n1-boot-choice', ['tools/test-q1n1-boot-choice.c'], []),
        ('test-a16-qcom-dwc3', ['tools/test-a16-qcom-dwc3.c', 'platform/uefi/qcom-dwc3.c'], []),
        ('test-q1n1-usb-ports-gadget', ['tools/test-q1n1-usb-ports.c', 'platform/uefi/usb-ports.c'], gadget),
        ('test-q1n1-gadget-ncm', ['tools/test-q1n1-gadget-ncm.c', 'platform/uefi/qcom-dwc3.c',
                                  'platform/uefi/ncm.c', 'platform/uefi/ncm-proxy.c'], gadget),
    ):
        binary = build / name
        subprocess.run(flags + defines + sources + ['-o', str(binary)], cwd=ROOT, check=True)
        subprocess.run([str(binary)], cwd=ROOT, check=True)
    subprocess.run([sys.executable, 'tools/test-q1n1-discovery.py'], cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
