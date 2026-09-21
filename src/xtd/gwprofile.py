#!/usr/bin/env python3
# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""gwprofile.py - save and restore a whole gateway configuration over USB CDC.

`gwcfg.py` sets one subsystem at a time. This reads or writes all of them
at once, through a plain JSON file you can keep in git, diff, and apply to
the next unit.

    gwprofile.py dump -o site-a.json        # read everything into a file
    gwprofile.py diff site-a.json           # what differs from the device
    gwprofile.py restore site-a.json -n     # show what would be written
    gwprofile.py restore site-a.json        # write it

The one thing this tool cannot do, and says so loudly rather than
pretending: **secrets are write-only on this interface.** The Wi-Fi PSK,
the ThingsBoard tokens and the DECT master key cannot be read back by any
transport -- that is a deliberate property of the config plane, not a gap
here. A dump therefore records them as null. On restore they are skipped
unless you supply them, because writing the empty string would not
"restore nothing", it would erase the live secret. That failure is silent
until the device next tries to associate, which is exactly the kind of
delayed damage a backup tool must not cause.
"""

import argparse
import datetime
import json
import os
import sys

from . import gwcfg as G  # noqa: E402


# name -> (command id, settable keys, secret keys, read-only keys)
#
# "settable" is the intersection of what a read reports and what a write
# accepts. They are not the same set: a read of `wifi` also returns the
# live IP and RSSI, which are observations, and a write of `dect --role`
# takes an ft_carrier that no read reports back. Anything outside the
# intersection cannot make a round trip, so it is classified rather than
# quietly dropped.
SUBSYS = {
    "wifi":  dict(id=G.ID_WIFI_CFG,  set=["ssid", "sec", "band", "enabled"],
                  secret=["psk"],
                  ro=["state", "rssi", "channel", "ip4", "ip6", "mask4", "gw4"]),
    "ip":    dict(id=G.ID_WIFI_IP,   set=["mode", "family", "addr", "mask", "gw"],
                  secret=[], ro=[]),
    "adv":   dict(id=G.ID_WIFI_ADV,  set=["ps", "reg"], secret=[], ro=[]),
    "cloud": dict(id=G.ID_CLOUD,     set=["host", "port", "interval", "dtls"],
                  secret=["token", "pkey", "psec"],
                  ro=["addr", "configured", "prov_configured", "posts", "acked",
                      "failed", "attr_posts", "node_posts", "node_failed",
                      "provisioned", "prov_failed", "plan_published",
                      "topo_overflow", "last_err", "dtls_hs", "dtls_hs_fail",
                      "rpc_reg", "rpc_sub", "rpc_rx", "rpc_ok", "rpc_err"]),
    "radio": dict(id=G.ID_DECT_RADIO, set=["mcs", "txpower", "chain", "cb_period",
                                           "scan_ms"], secret=[], ro=[]),
    "led":   dict(id=G.ID_LED,       set=["en", "lv", "br", "fl", "gr", "gg", "gb"],
                  secret=[], ro=[]),
    "usb":   dict(id=G.ID_USB_CFG,   set=["audio"], secret=[],
                  ro=["active", "reboot_required"]),
    "br":    dict(id=G.ID_BR,        set=["prefix", "enabled"], secret=[],
                  ro=["mcast"]),
    "vpn":   dict(id=G.ID_VPN,       set=["endpoint", "port", "server_pub",
                                          "my_addr", "allowed", "keepalive",
                                          "enabled"],
                  secret=[], ro=["my_pub", "up"]),
    "sec":   dict(id=G.ID_DECT_SEC,  set=["require"], secret=["key"],
                  ro=["profile_has_key", "known", "link"]),
    "dect":  dict(id=G.ID_DECT,      set=["network", "carrier", "nw_carrier",
                                          "sink", "autostart", "hop", "parent", "sense"],
                  secret=[],
                  ro=["role", "role_carrier", "hop_stored", "sr_hop", "link",
                      "ft_up", "awaiting", "is_sink", "mac_role", "phy_ready",
                      "boot_prof"]),
}

# `wifi` needs the PSK in the same write, so it is skipped without one --
# see the note at the top. The others take partial writes.
NEEDS_SECRET = {"wifi": "psk"}

# Writing these reshapes a running mesh rather than a setting: a `dect`
# write can restart the FT, and publishing a plan bumps CDC_SEQ so every
# node re-adopts it. Opt in per run; a restore that quietly re-roled a
# live sink would be worse than one that did too little.
DISRUPTIVE = {"dect", "plan"}

SECRET_HELP = {
    "psk": "--psk",
    "token": "--token",
    "pkey": "--pkey",
    "psec": "--psec",
    "key": "--dect-key (32 hex digits)",
}


def read_all(dev, quiet=False):
    out, status = {}, {}
    for name, spec in SUBSYS.items():
        try:
            r = G.command(dev, G.OP_READ, spec["id"], {})
        except Exception as e:
            if not quiet:
                print("  %-6s unreadable: %s" % (name, e), file=sys.stderr)
            continue
        out[name] = {k: r[k] for k in spec["set"] if k in r}
        for k in spec["secret"]:
            out[name][k] = None
        status[name] = {k: r[k] for k in r if k not in spec["set"]
                        and k not in spec["secret"]}
    try:
        p = G.command(dev, G.OP_READ, G.ID_CDC_PLAN, {}).get("plan", "")
        if p:
            out["plan"] = p
    except Exception as e:
        if not quiet:
            print("  plan   unreadable: %s" % e, file=sys.stderr)
    return out, status


def cmd_dump(dev, ns):
    settings, status = read_all(dev)
    try:
        sysinfo = G.command(dev, G.OP_READ, G.ID_SYS, {})
    except Exception:
        sysinfo = {}
    prof = {
        "_meta": {
            "tool": "gwprofile.py",
            "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "device": sysinfo,
            "note": "Secrets read back as null: this interface never returns "
                    "them. Fill them in by hand, or pass them to `restore` on "
                    "the command line. A null secret is SKIPPED on restore, "
                    "never written as an empty string.",
        },
        "settings": settings,
    }
    if ns.include_status:
        prof["status"] = status
    text = json.dumps(prof, indent=2, ensure_ascii=False, sort_keys=True)
    if ns.out:
        with open(ns.out, "w") as f:
            f.write(text + "\n")
        missing = [(s, k) for s, v in settings.items() if isinstance(v, dict)
                   for k, x in v.items() if x is None]
        print("wrote %s (%d subsystems)" % (ns.out, len(settings)))
        if missing:
            print("\nnot in the file, because the device will not reveal them:")
            for s, k in missing:
                print("  %-6s %-6s -> supply with %s" % (s, k, SECRET_HELP.get(k, "--" + k)))
    else:
        print(text)
    return 0


def flatten(settings):
    flat = {}
    for s, v in settings.items():
        if isinstance(v, dict):
            for k, x in v.items():
                flat[(s, k)] = x
        else:
            flat[(s, None)] = v
    return flat


def cmd_diff(dev, ns):
    want = json.load(open(ns.file)).get("settings", {})
    live, _ = read_all(dev, quiet=True)
    a, b = flatten(want), flatten(live)
    rows = []
    for key in sorted(set(a) | set(b), key=lambda t: (t[0], t[1] or "")):
        fv, dv = a.get(key, "<absent>"), b.get(key, "<absent>")
        if fv is None:
            continue  # a secret the file does not carry says nothing
        if key[0] == "plan" and isinstance(fv, str) and isinstance(dv, str):
            if json_equal(fv, dv):
                continue
            fv, dv = "<plan %d B>" % len(fv), "<plan %d B>" % len(dv)
        if fv != dv:
            rows.append((key, dv, fv))
    if not rows:
        print("device matches %s" % ns.file)
        return 0
    print("%-14s %-24s %s" % ("setting", "device", "file"))
    for (s, k), dv, fv in rows:
        print("%-14s %-24s %s" % ("%s.%s" % (s, k) if k else s, dv, fv))
    return 1


def json_equal(a, b):
    try:
        return json.loads(a) == json.loads(b)
    except ValueError:
        return a == b


def cmd_restore(dev, ns):
    prof = json.load(open(ns.file))
    want = prof.get("settings", {})
    supplied = {"psk": ns.psk, "token": ns.token, "pkey": ns.pkey,
                "psec": ns.psec, "key": ns.dect_key}

    only = set(x.strip() for x in ns.only.split(",")) if ns.only else None
    extra = set(x.strip() for x in ns.include.split(",")) if ns.include else set()

    planned, skipped = [], []
    for name, spec in SUBSYS.items():
        if name not in want:
            continue
        if only is not None and name not in only:
            continue
        if name in DISRUPTIVE and name not in extra and only is None:
            skipped.append((name, "reshapes a running mesh; add --include %s" % name))
            continue
        req = {}
        for k in spec["set"]:
            if k in want[name] and want[name][k] is not None:
                req[k] = want[name][k]
        for k in spec["secret"]:
            v = supplied.get(k) or want[name].get(k)
            if v is not None:
                req[k] = bytes.fromhex(v) if k == "key" else v
        need = NEEDS_SECRET.get(name)
        if need and need not in req:
            skipped.append((name, "needs %s, which no dump can contain; pass %s"
                            % (need, SECRET_HELP.get(need, "--" + need))))
            continue
        # The device treats `enabled` on read as `enable` on write for Wi-Fi.
        if name == "wifi" and "enabled" in req:
            req["enable"] = req.pop("enabled")
        if req:
            planned.append((name, spec["id"], req))

    if "plan" in want and ("plan" in extra or (only and "plan" in only)):
        planned.append(("plan", G.ID_CDC_PLAN, {"plan": want["plan"]}))
    elif "plan" in want:
        skipped.append(("plan", "republishing bumps CDC_SEQ and every node "
                                "re-adopts; add --include plan"))

    for name, why in skipped:
        print("skip  %-6s %s" % (name, why))
    for name, _, req in planned:
        shown = {k: ("<%d B>" % len(v) if isinstance(v, (bytes, bytearray)) else v)
                 for k, v in req.items()}
        print("write %-6s %s" % (name, shown))
    if ns.dry_run:
        print("\n-n given: nothing was written")
        return 0
    if not planned:
        print("\nnothing to write")
        return 0

    rc = 0
    for name, cid, req in planned:
        try:
            G.command(dev, G.OP_WRITE, cid, req, timeout=10.0)
            print("  ok   %s" % name)
        except Exception as e:
            print("  FAIL %s: %s" % (name, e))
            rc = 1
    return rc


def cmd_site(dev, ns):
    """The site as it is, not as it was configured.

    A plan says what was intended; this says what actually happened -- who
    each node ended up attached to, how long it has been up, when it last
    spoke. Commissioning needs both, and they are not the same document:
    the interesting sites are the ones where they disagree.

    Read-only by nature. There is no `restore` for it: you cannot write a
    mesh's live state back onto it, only the plan that shapes it.
    """
    nodes = G.command(dev, G.OP_READ, G.ID_SENSE, {}).get("nodes", [])
    try:
        plan = G.command(dev, G.OP_READ, G.ID_CDC_PLAN, {}).get("plan", "")
    except Exception:
        plan = ""
    try:
        dect = G.command(dev, G.OP_READ, G.ID_DECT, {})
    except Exception:
        dect = {}

    def hx(v):
        return "%08x" % v if isinstance(v, int) else v

    entries = []
    for n in nodes:
        e = dict(n)
        e["rdid"] = hx(n.get("rdid"))
        # 0 means the node reports no parent: a sink, or a node whose
        # firmware predates the v3 wire. Written as null rather than
        # "00000000", which would read like a real address.
        p = n.get("parent", 0)
        e["parent"] = hx(p) if p else None
        entries.append(e)

    snap = {
        "_meta": {
            "tool": "gwprofile.py site",
            "captured": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": "Live state, not configuration. `parent` is each node's own "
                    "answer to which FT it is attached to (nrsense v3), not the "
                    "sink's view of it -- when the two disagree, that disagreement "
                    "is the finding.",
        },
        "sink": {k: dect.get(k) for k in ("network", "carrier", "nw_carrier",
                                          "sink", "role", "hop", "autostart")},
        "plan": json.loads(plan) if plan else None,
        "nodes": sorted(entries, key=lambda e: e["rdid"]),
    }
    text = json.dumps(snap, indent=2, ensure_ascii=False)
    if ns.out:
        with open(ns.out, "w") as f:
            f.write(text + "\n")
        live = [e for e in entries if e.get("age", 0) < 180]
        print("wrote %s: %d nodes, %d reporting" % (ns.out, len(entries), len(live)))
        for e in snap["nodes"]:
            print("  %-8s parent %-8s age %4ss" % (e["rdid"], e["parent"] or "-",
                                                   e.get("age", "?")))
    else:
        print(text)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-d", "--dev", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="read every setting into a JSON file")
    d.add_argument("-o", "--out", default=None, help="file to write (default: stdout)")
    d.add_argument("--include-status", action="store_true",
                   help="also record live observations (IP, RSSI, counters) "
                        "for reference; they are never restored")

    df = sub.add_parser("diff", help="what differs between a file and the device")
    df.add_argument("file")

    st = sub.add_parser("site", help="snapshot the site as it is right now "
                                     "(plan + who is attached to whom + health)")
    st.add_argument("-o", "--out", default=None, help="file to write (default: stdout)")

    r = sub.add_parser("restore", help="write a saved profile back")
    r.add_argument("file")
    r.add_argument("-n", "--dry-run", action="store_true")
    r.add_argument("--only", default=None, metavar="LIST",
                   help="restore only these subsystems, comma separated")
    r.add_argument("--include", default=None, metavar="LIST",
                   help="also restore the disruptive ones: dect, plan")
    r.add_argument("--psk", default=None, help="Wi-Fi PSK (not in any dump)")
    r.add_argument("--token", default=None, help="ThingsBoard device token")
    r.add_argument("--pkey", default=None, help="TB provision key")
    r.add_argument("--psec", default=None, help="TB provision secret")
    r.add_argument("--dect-key", default=None, help="DECT master key, 32 hex digits")

    ns = ap.parse_args()
    dev = ns.dev or G.default_dev()
    return {"dump": cmd_dump, "diff": cmd_diff, "restore": cmd_restore,
            "site": cmd_site}[ns.cmd](dev, ns)


if __name__ == "__main__":
    sys.exit(main())
