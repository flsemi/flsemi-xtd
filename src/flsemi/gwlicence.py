# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""gwlicence.py - read, request and install a device licence.

A licence is signed by FLSEMI over the chip's own FICR DEVICEID and verified on
the chip against a public key in its secure image. The device holds no secret:
a licence copied to another unit fails because the device id inside it is not
that unit's, and forging one needs a private key that never leaves our signing
machine.

This tool never signs. Issuing is a process on our side; here we only read what
a unit has, produce the request that asks for one, and install what comes back.
Format: doc/licence-format.md.
"""
import argparse
import binascii
import json
import struct
import sys
import time

from . import gwcfg as G

ID_LICENCE = 29          # proposed; see doc/licence-format.md

MAGIC = 0x464C4943       # 'FLIC'
VERSION = 1
LEN = 112
BODY = 48                # the signed part
CHIP = {1: "nRF9151", 2: "nRF5340"}
CHIP_ARG = {"nrf9151": 1, "nrf5340": 2}


def parse(blob: bytes) -> dict:
    """Read a licence the way the chip does, and refuse the same things."""
    if len(blob) != LEN:
        raise ValueError("a licence is %d octets, this is %d" % (LEN, len(blob)))
    magic, ver, chip, flags = struct.unpack_from(">IBBH", blob, 0)
    if magic != MAGIC:
        raise ValueError("not a licence (magic %08x)" % magic)
    if ver != VERSION:
        raise ValueError("licence version %d, this tool knows version %d" % (ver, VERSION))
    dev_id = blob[8:16]
    line, issued, expires = struct.unpack_from(">III", blob, 16)
    order = blob[28:44].rstrip(b"\0").decode("ascii", "replace")
    return {
        "chip": chip, "chip_name": CHIP.get(chip, "chip %d" % chip),
        "evaluation": bool(flags & 1), "flags": flags,
        "device_id": dev_id.hex(),
        "product_line": line, "issued": issued, "expires": expires,
        "order": order, "signature": blob[48:].hex(),
    }


def body(chip, device_id_hex, product_line, issued, expires, order, evaluation=False):
    """The 48 signed octets. Here so that whatever signs them uses this
    definition rather than a second copy that drifts from it."""
    dev = bytes.fromhex(device_id_hex)
    if len(dev) != 8:
        raise ValueError("a FICR DEVICEID is 8 octets, got %d" % len(dev))
    order_b = order.encode("ascii")[:16].ljust(16, b"\0")
    return (struct.pack(">IBBH", MAGIC, VERSION, chip, 1 if evaluation else 0)
            + dev + struct.pack(">III", product_line, issued, expires)
            + order_b + b"\0" * 4)


def stamp(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t)) if t else "perpetual"


def show(lic, prefix=""):
    print("%s%s  device %s  %s" % (prefix, lic["chip_name"], lic["device_id"],
                                   "EVALUATION" if lic["evaluation"] else "full"))
    print("%s  product line %s, issued %s, expires %s"
          % (prefix, ("0x%08x" % lic["product_line"]) if lic["product_line"] else "any",
             stamp(lic["issued"]), stamp(lic["expires"])))
    if lic["order"]:
        print("%s  order %s" % (prefix, lic["order"]))


def device_identity(dev):
    chip = G.command(dev, G.OP_READ, G.ID_CHIPID, {}, timeout=6.0)
    ota = {}
    try:
        ota = G.command(dev, G.OP_READ, G.ID_OTA, {}, timeout=6.0)
    except Exception:
        pass
    return {
        "nrf9151_device_id": chip.get("modem_dev_id", ""),
        "nrf5340_device_id": chip.get("dev_id", ""),
        "rd_id": "%08x" % (chip.get("modem_id") or 0),
        "product_line": ota.get("board") or ota.get("image_type") or 0,
    }


def main():
    ap = argparse.ArgumentParser(
        description="Read, request or install this unit's licence.",
        epilog="A licence is bound to the chip's FICR device id, which is not "
               "writable: it cannot be moved to another unit.")
    ap.add_argument("-d", "--dev", default=None, help="the kit's SMP port, or ble:<rd id>")
    ap.add_argument("--request", metavar="FILE", nargs="?", const="-",
                    help="write the licence request for this unit (default: stdout)")
    ap.add_argument("--install", metavar="FILE", help="install a licence issued for this unit")
    ap.add_argument("--show", metavar="FILE", help="read a licence file without a device")
    ap.add_argument("--chip", choices=sorted(CHIP_ARG), default="nrf9151",
                    help="which die the request is for (default: the nRF9151, where the stack runs)")
    args = ap.parse_args()

    if args.show:
        with open(args.show, "rb") as f:
            show(parse(f.read()))
        return 0

    dev = args.dev or G.default_dev()
    me = device_identity(dev)

    if args.request:
        req = {
            "schema": 1,
            "requested": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "chip": args.chip,
            "device_id": me["nrf9151_device_id"] if args.chip == "nrf9151"
                         else me["nrf5340_device_id"],
            "product_line": "0x%08x" % me["product_line"] if me["product_line"] else None,
            "rd_id": me["rd_id"],
        }
        if not req["device_id"]:
            sys.exit("flsemi licence: this unit does not report a %s device id; "
                     "a licence cannot be bound without it" % args.chip)
        out = json.dumps(req, indent=2)
        if args.request == "-":
            print(out)
        else:
            open(args.request, "w").write(out + "\n")
            print("wrote %s -- send it to FLSEMI; it carries no secret" % args.request)
        return 0

    if args.install:
        with open(args.install, "rb") as f:
            blob = f.read()
        lic = parse(blob)          # refuse a malformed file before the device sees it
        want = me["nrf9151_device_id"] if lic["chip"] == 1 else me["nrf5340_device_id"]
        if want and lic["device_id"].lower() != want.lower():
            sys.exit("flsemi licence: this licence is for %s %s, and this unit is %s.\n"
                     "A licence is bound to the die and cannot be moved."
                     % (lic["chip_name"], lic["device_id"], want))
        if lic["expires"] and lic["expires"] < time.time():
            print("warning: this licence expired on %s" % stamp(lic["expires"]),
                  file=sys.stderr)
        show(lic, "installing: ")
        try:
            r = G.command(dev, G.OP_WRITE, ID_LICENCE, {"blob": blob}, timeout=10.0)
        except RuntimeError as e:
            if "rc=8" in str(e):
                sys.exit("flsemi licence: this unit's firmware does not serve the "
                         "licence endpoint yet (see doc/licence-format.md)")
            raise
        print(r)
        return 0

    # no action: report what the unit has
    try:
        r = G.command(dev, G.OP_READ, ID_LICENCE, {}, timeout=6.0)
    except RuntimeError as e:
        if "rc=8" in str(e) or "rc=5" in str(e):
            print("this unit's firmware does not serve the licence endpoint "
                  "(gwcfg id %d) -- nothing is enforced on it." % ID_LICENCE)
            print("nRF9151 device id %s\nnRF5340 device id %s\nproduct line %s"
                  % (me["nrf9151_device_id"], me["nrf5340_device_id"],
                     ("0x%08x" % me["product_line"]) if me["product_line"] else "unknown"))
            return 1
        raise
    print(r)
    return 0
