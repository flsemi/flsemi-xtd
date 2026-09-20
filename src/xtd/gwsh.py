# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""Gateway shell helper: send a command, return the hexdump bytes.

The parser below is the fix for a cap that never existed: shell_hexdump()
puts a DOUBLE space after octet 8, so a naive "([0-9a-f]{2} ?)+" stops at
byte 8 and every read looks capped at 8 octets.  Split on the '|' instead.
"""
import serial, time, re, sys

class GwSh:
    def __init__(self, dev='/dev/cu.usbmodem13303', baud=115200):
        self.s = serial.Serial(dev, baud, timeout=1)
        time.sleep(0.4)

    def cmd(self, c, w=1.6, cap=40000):
        self.s.reset_input_buffer()
        self.s.write(('\r%s\r' % c).encode())
        time.sleep(w)
        return self.s.read(cap).decode('utf-8', 'replace')

    @staticmethod
    def parse_hexdump(out):
        b = []
        for line in out.splitlines():
            line = line.strip()
            m = re.match(r'^([0-9a-f]{8}): (.*?)\s*\|', line)
            if not m:
                continue
            b += [int(x, 16) for x in m.group(2).split()]
        return bytes(b)

    def read(self, addr, n=4, w=1.6):
        return self.parse_hexdump(self.cmd('hif r 0x%04X %d' % (addr, n), w))

    def close(self):
        self.s.close()
