# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""gwupdate.py - fetch the firmware published for THIS kit and install it.

The tool moves bytes it cannot read. Published images are encrypted to a key
that exists only in the devices we sold, and the decryption happens inside
MCUboot during the swap -- so the artifact can sit in a public release without
being of use to anyone else, and nothing on this computer ever holds the
plaintext. See doc/firmware-distribution.md.

That property is also why this command is finished before the firmware work
that makes it matter: verifying a digest and uploading bytes is the same job
whether or not those bytes are encrypted.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request

from . import gwcfg as G

# Where releases are published. Nothing is there yet: publishing images before
# the encryption and signing work lands would be publishing the firmware.
MANIFEST_URL = "https://raw.githubusercontent.com/flsemi/flsemi/main/releases/manifest.json"

CHIP_5340, CHIP_9151 = "nrf5340", "nrf9151"


def fetch(url, timeout=30):
    if "://" not in url or url.startswith("file://"):
        with open(url.replace("file://", ""), "rb") as f:
            return f.read()
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def describe_device(dev):
    """What this kit is, in the terms the manifest matches on."""
    chip = G.command(dev, G.OP_READ, G.ID_CHIPID, {}, timeout=6.0)
    sysv = G.command(dev, G.OP_READ, G.ID_SYS, {}, timeout=6.0)
    ota = {}
    try:
        ota = G.command(dev, G.OP_READ, G.ID_OTA, {}, timeout=6.0)
    except Exception:
        pass          # a gateway built without the node image store
    return {
        "dev_id": chip.get("dev_id", ""),
        "modem_dev_id": chip.get("modem_dev_id", ""),
        "rdid": "%08x" % (chip.get("modem_id") or 0),
        "gw_version": sysv.get("ver", "?"),
        "image_type": ota.get("board") or ota.get("image_type"),
        "node_build": ota.get("running_id"),
    }


def pick(manifest, chip, image_type):
    """The one artifact for this kit, or a refusal that says what was wrong.

    Deliberately never falls back to 'the only one there is'. `image_type` is
    the board target and layout, and under one encryption key per product line
    it is also what says which artifact this kit holds the key for: the wrong
    one uploads, fails to decrypt and does not boot, which is a site visit.
    Refusing is the cheaper answer.
    """
    arts = [a for a in manifest.get("artifacts", []) if a.get("chip") == chip]
    if not arts:
        return None, "the manifest publishes nothing for the %s" % chip
    if image_type is None:
        if len(arts) > 1:
            return None, ("this kit does not report its image type and the "
                          "release carries several %s images -- update the "
                          "gateway first, or name the file with --file" % chip)
        return arts[0], None
    want = "0x%08x" % image_type
    arts = [a for a in arts if str(a.get("image_type", "")).lower() == want]
    if not arts:
        return None, ("the manifest has no %s image of type %s -- this kit "
                      "reports that type and holds the key for no other" % (chip, want))
    if len(arts) > 1:
        return None, ("the release offers %d %s images of type %s; it should "
                      "offer one. Refusing rather than choosing" % (len(arts), chip, want))
    return arts[0], None


def main():
    ap = argparse.ArgumentParser(
        description="Install the firmware published for this kit.",
        epilog="The published image is encrypted to the kit's product line; "
               "this tool never decrypts it, and an image published for "
               "another product line will not boot.")
    ap.add_argument("-d", "--dev", default=None, help="the kit's SMP port, or ble:<rd id>")
    ap.add_argument("--chip", choices=[CHIP_5340, CHIP_9151], default=CHIP_5340,
                    help="which processor to update (default: the gateway's nRF5340)")
    ap.add_argument("--manifest", default=MANIFEST_URL,
                    help="release manifest (URL, or a local file for an offline site)")
    ap.add_argument("--file", default=None,
                    help="install this local artifact instead of downloading "
                         "(still checked against the manifest's digest)")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="say what would be installed and stop")
    ap.add_argument("--chunk", type=int, default=0)
    args = ap.parse_args()

    dev = args.dev or G.default_dev()
    me = describe_device(dev)
    print("kit %s  gateway %s  image type %s"
          % (me["rdid"], me["gw_version"],
             ("0x%08x" % me["image_type"]) if me["image_type"] else "unknown"))

    try:
        manifest = json.loads(fetch(args.manifest))
    except Exception as e:
        sys.exit("flsemi update: cannot read the release manifest (%s): %s"
                 % (args.manifest, e))

    art, why = pick(manifest, args.chip, me["image_type"])
    if art is None:
        sys.exit("flsemi update: %s" % why)

    have = me["gw_version"] if args.chip == CHIP_5340 else None
    print("published: %s %s  %s  %d B%s"
          % (art.get("chip"), art.get("version"), art.get("file"),
             art.get("size", 0), "  (encrypted)" if art.get("encrypted") else ""))
    if have and str(art.get("version")) == str(have):
        print("this kit already runs that version")
    if args.dry_run:
        return 0

    if args.file:
        blob = open(args.file, "rb").read()
    else:
        base = args.manifest.rsplit("/", 1)[0]
        url = art.get("url") or (base + "/" + art["file"])
        print("downloading %s" % url)
        blob = fetch(url, timeout=300)

    digest = hashlib.sha256(blob).hexdigest()
    if art.get("sha256") and digest != art["sha256"].lower():
        sys.exit("flsemi update: the artifact does not match the manifest digest "
                 "(got %s). Nothing was written to the kit." % digest[:16])
    if art.get("size") and len(blob) != art["size"]:
        sys.exit("flsemi update: the artifact is %d B, the manifest says %d. "
                 "Nothing was written to the kit." % (len(blob), art["size"]))
    print("sha256 ok (%s…)" % digest[:16])

    if args.chip == CHIP_5340:
        slots = G.img_upload(dev, blob, args.chunk, G._progress_stderr)
        sys.stderr.write("\n")
        new = [s for s in slots if s["slot"] == 1]
        run = [s for s in slots if s["slot"] == 0]
        if new and run and new[0]["hash"] == run[0]["hash"]:
            print("same image as the one running; nothing to swap")
            return 0
        if not new:
            sys.exit("no image in slot 1 after the upload")
        G.img_test(dev, new[0]["hash"])
        G.os_reset(dev)
        print("marked for test boot and reset. When it is back:\n"
              "  flsemi cfg -d %s dfu --confirm" % dev)
    else:
        # the 9151 goes through the gateway's serial-recovery proxy; MCUboot
        # on the 9151 is what decrypts it
        G.dfu91_upload(dev, blob, 0, args.chunk or 896, 0, G._progress_stderr)
        sys.stderr.write("\n")
        print("uploaded to the nRF9151 through the gateway's recovery proxy")
    return 0
