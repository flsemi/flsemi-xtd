# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""One entry point, several sub-tools. Each sub-tool keeps its own argparse; we only strip the first word.

No sub-tool names a product. The device on the port already says what it is --
`probe` asks it, and `update` picks an artifact by the image type the device
reports rather than by anything a human typed. Asking the human to say it as
well would only add a second answer that can disagree with the first.
"""
import sys

USAGE = """usage: flsemi <tool> [args]

  ui        local browser dashboard        (flsemi ui -d /dev/cu.usbmodemXXXX1)
  stage     local demo wall: mesh + sensors (flsemi stage -d /dev/cu.usbmodemXXXX1)
  update    install the firmware published for this kit
  licence   read, request or install this unit's licence
  cfg       configuration & firmware       (flsemi cfg --help)
  profile   whole-device dump/diff/restore (flsemi profile dump out.json)
  sh        raw shell helper               (flsemi sh 'hif r 0x0028 4')
  version

Transport for every tool: -d <CDC-ACM#0 port>  or  -d ble:<name or rd-id>
"""

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE); return 0
    tool, rest = argv[0], argv[1:]
    # the sub-tools want -d before their own subcommand; accept it anywhere
    for flag in ("-d", "--dev"):
        if flag in rest[1:]:
            i = rest.index(flag)
            rest = rest[i:i + 2] + rest[:i] + rest[i + 2:]
    if tool == "version":
        from . import __version__; print(__version__); return 0
    if tool == "ui":
        from . import gwui as m
    elif tool == "stage":
        from . import gwstage as m
    elif tool == "update":
        from . import gwupdate as m
    elif tool in ("licence", "license"):
        from . import gwlicence as m
    elif tool == "cfg":
        from . import gwcfg as m
    elif tool == "profile":
        from . import gwprofile as m
    elif tool == "sh":
        from .gwsh import GwSh
        if not rest: print("usage: flsemi sh [-d PORT] '<command>'"); return 2
        dev = None
        if rest[0] == "-d": dev, rest = rest[1], rest[2:]
        g = GwSh(dev) if dev else GwSh()
        try: print(g.cmd(" ".join(rest)))
        finally: g.close()
        return 0
    else:
        print(USAGE); return 2
    sys.argv = ["flsemi " + tool] + rest
    try:
        return m.main()
    except (RuntimeError, TimeoutError, OSError, ValueError) as e:
        print("flsemi %s: %s" % (tool, e), file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
