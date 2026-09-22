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

    _ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
    _LOG = re.compile(r'^\[\d\d:\d\d:\d\d\.\d+,\d+\] <\w+> ')

    def cmd(self, c, w=1.6, cap=40000):
        """Send one shell command; return its answer with the prompt echoes
        and the gateway's own log stream (which shares the port) stripped."""
        self.s.reset_input_buffer()
        self.s.write(('\r%s\r' % c).encode())
        time.sleep(w)
        raw = self.s.read(cap).decode('utf-8', 'replace')
        out, seen = [], False
        for line in self._ANSI.sub('', raw).splitlines():
            line = line.replace('gw:~$', '').strip()
            if line == c:          # the echo: everything before it is stale
                seen, out = True, []
                continue
            if not seen or not line or self._LOG.match(line):
                continue
            out.append(line)
        return '\n'.join(out)

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
        """Read n octets, or say why fewer came back.

        A short read is the signature of both defects this parser has been
        bitten by: a hexdump chopped by the double space after octet 8, and
        the 9151's own 120-octet reply ceiling, where the host is expected to
        continue reading and a host that does not gets a buffer whose tail is
        stale data from the previous reply. Neither announces itself -- the
        bytes that do arrive decode into entirely plausible values -- so the
        length is checked here rather than trusted by every caller.
        """
        out = self.parse_hexdump(self.cmd('hif r 0x%04X %d' % (addr, n), w))
        if len(out) != n:
            raise ValueError(
                "short read at 0x%04X: asked for %d octets, parsed %d. "
                "%s Do not use the bytes: a partial row decodes into values "
                "that look real."
                % (addr, n, len(out),
                   "The 9151 answers at most 120 octets per reply and says so "
                   "in its length field -- read the rest in a second request."
                   if n > 120 else
                   "Either the reply was truncated or the hexdump was not "
                   "parsed whole."))
        return out

    def close(self):
        self.s.close()
