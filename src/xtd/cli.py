# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""One entry point, four sub-tools. Each sub-tool keeps its own argparse; we only strip the first word."""
import sys

USAGE = """usage: xtd <tool> [args]

  ui        local browser dashboard        (xtd ui -d /dev/cu.usbmodemXXXX1)
  cfg       configuration & firmware       (xtd cfg dect --help)
  profile   whole-device dump/diff/restore (xtd profile dump out.json)
  sh        raw shell helper               (xtd sh 'hif r 0x0028 4')
  version

Transport for every tool: -d <CDC-ACM#0 port>  or  -d ble:<name or rd-id>
"""

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE); return 0
    tool, rest = argv[0], argv[1:]
    if tool == "version":
        from . import __version__; print(__version__); return 0
    if tool == "ui":
        from . import gwui as m
    elif tool == "cfg":
        from . import gwcfg as m
    elif tool == "profile":
        from . import gwprofile as m
    elif tool == "sh":
        from .gwsh import GwSh
        if not rest: print("usage: xtd sh [-d PORT] '<command>'"); return 2
        dev = None
        if rest[0] == "-d": dev, rest = rest[1], rest[2:]
        g = GwSh(dev) if dev else GwSh()
        try: print(g.cmd(" ".join(rest)))
        finally: g.close()
        return 0
    else:
        print(USAGE); return 2
    sys.argv = ["xtd " + tool] + rest
    return m.main()

if __name__ == "__main__":
    sys.exit(main())
