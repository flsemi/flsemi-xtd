#!/usr/bin/env python3
# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""gwui.py - a local browser UI for the USB CDC configuration plane.

    /usr/bin/python3 tools/gwui.py            # then open http://127.0.0.1:8765

It runs on the machine with the cable, because that is where the serial
port is. Nothing here talks to the network: the page is served from
localhost and every value on it came off the wire from the device in
front of you.

Two things it does that the CLI cannot:

* **It owns the port.** Every request is serialised behind one lock and
  one open connection. Running two `gwcfg.py` invocations back to back
  used to fight over the port and produce timeouts that looked like a
  dead board; a UI that polls would do that to itself constantly.
* **It shows attitude live**, which needs the device, not the cloud.

Transport: -d /dev/cu.usbmodemXXXX1 (the SMP CDC port) or -d ble:<rdid>
once the device has opened its BLE window (Button 2 double press); on
macOS run it from Terminal.app for the Bluetooth permission. The page then
does everything the cable does, firmware for both chips included.

Read-only by default. Writes are refused unless started with --allow-write,
because a configuration page that a stray click can change is a different
kind of tool from one you can leave open on a bench.
"""

import argparse
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import gwcfg as G  # noqa: E402

PORT_LOCK = threading.Lock()
DEV = None
ALLOW_WRITE = False

# name -> (command id, writable)
SUBSYS = {
    "status": (G.ID_STATUS, False),
    "sys":    (G.ID_SYS, True),
    "wifi":   (G.ID_WIFI_CFG, True),
    "ip":     (G.ID_WIFI_IP, False),
    "adv":    (G.ID_WIFI_ADV, True),
    "cloud":  (G.ID_CLOUD, True),
    "dect":   (G.ID_DECT, True),
    "sec":    (G.ID_DECT_SEC, True),
    "radio":  (G.ID_DECT_RADIO, True),
    "geo":    (G.ID_GEO, True),
    "led":    (G.ID_LED, True),
    "br":     (G.ID_BR, True),
    "vpn":    (G.ID_VPN, True),
    "usb":    (G.ID_USB_CFG, True),
    "motion": (24, False),
    "chipid": (25, False),
    "inventory": (26, False),
    "sense":  (G.ID_SENSE, False),
    "plan":   (G.ID_CDC_PLAN, True),
    "hif":    (G.ID_HIF, True),
    "ota":    (27, True),
    "ble":    (28, True),
    "dfu91":  (G.ID_DFU91, True),
}

# Firmware upload state, one at a time (the port is one, and so is the
# 9151's recovery slot). Polled by the page.
DFU = {"busy": False, "what": "", "off": 0, "total": 0, "rate": 0.0, "result": None, "error": None}


def talk(op, cmd_id, payload, timeout=6.0):
    with PORT_LOCK:
        return G.command(DEV, op, cmd_id, payload, timeout)


def to_jsonable(o):
    if isinstance(o, bytes):
        return o.hex()
    if isinstance(o, dict):
        return {k: to_jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [to_jsonable(v) for v in o]
    return o


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # the console is for errors, not for a polling UI's traffic

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if path == "/api/meta":
            self._send(200, json.dumps({"dev": DEV, "write": ALLOW_WRITE,
                                        "subsys": sorted(SUBSYS),
                                        "writable": sorted(k for k, v in SUBSYS.items() if v[1])}))
            return
        if path == "/api/dfu":
            self._send(200, json.dumps(DFU))
            return
        if path == "/api/slots":
            try:
                with PORT_LOCK:
                    self._send(200, json.dumps(G.img_slots(DEV)))
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)}))
            return
        if path.startswith("/api/read/"):
            name = path[len("/api/read/"):]
            if name not in SUBSYS:
                self._send(404, json.dumps({"error": "unknown subsystem"}))
                return
            try:
                r = talk(G.OP_READ, SUBSYS[name][0], {})
                self._send(200, json.dumps(to_jsonable(r)))
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)}))
            return
        self._send(404, json.dumps({"error": "no such path"}))

    def _dfu_start(self, what, blob, opts):
        """Run an upload on its own thread; the page polls /api/dfu."""
        if DFU["busy"]:
            self._send(409, json.dumps({"error": "an upload is already running"}))
            return

        def progress(off, total, t0):
            import time
            DFU.update(off=off, total=total, rate=off / max(time.time() - t0, 0.001) / 1024)

        def run():
            try:
                with PORT_LOCK:
                    if what == "5340":
                        slots = G.img_upload(DEV, blob, 0, progress)
                        if opts.get("test"):
                            new = [sl for sl in slots if sl["slot"] == 1]
                            if new:
                                slots = G.img_test(DEV, new[0]["hash"])
                        if opts.get("reset"):
                            G.os_reset(DEV)
                        DFU["result"] = {"slots": slots, "reset": bool(opts.get("reset"))}
                    else:
                        r = G.dfu91_upload(DEV, blob, int(opts.get("target", 0)), 896,
                                           int(opts.get("baud", 0)), progress)
                        DFU["result"] = to_jsonable(r)
            except Exception as e:
                traceback.print_exc()
                DFU["error"] = str(e)
            finally:
                DFU["busy"] = False

        DFU.update(busy=True, what=what, off=0, total=len(blob), rate=0.0, result=None, error=None)
        threading.Thread(target=run, daemon=True).start()
        self._send(202, json.dumps({"started": what, "size": len(blob)}))

    def do_POST(self):
        if self.path in ("/api/dfu/5340", "/api/dfu/9151", "/api/confirm", "/api/reset"):
            if not ALLOW_WRITE:
                self._send(403, json.dumps({"error": "started read-only; restart with --allow-write"}))
                return
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/confirm":
                with PORT_LOCK:
                    self._send(200, json.dumps(G.img_confirm(DEV)))
                return
            if self.path == "/api/reset":
                with PORT_LOCK:
                    G.os_reset(DEV)
                self._send(200, json.dumps({"reset": True}))
                return
            import base64
            blob = base64.b64decode(body.get("image", ""))
            if len(blob) < 1024:
                self._send(400, json.dumps({"error": "no image in the request"}))
                return
            self._dfu_start("5340" if self.path.endswith("5340") else "9151", blob, body)
            return
        if not self.path.startswith("/api/write/"):
            self._send(404, json.dumps({"error": "no such path"}))
            return
        name = self.path[len("/api/write/"):]
        if name not in SUBSYS or not SUBSYS[name][1]:
            self._send(404, json.dumps({"error": "not writable"}))
            return
        if not ALLOW_WRITE:
            self._send(403, json.dumps({
                "error": "started read-only; restart with --allow-write"}))
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if name == "sec" and "key" in body:
                # 32 hex digits -> bytes, the same shape gwcfg.py sends. The
                # key is write-only at the far end: nothing here can read it
                # back, so nothing here pretends to.
                body["key"] = bytes.fromhex(body["key"].strip().replace(" ", ""))
            r = talk(G.OP_WRITE, SUBSYS[name][0], body, timeout=12.0)
            self._send(200, json.dumps(to_jsonable(r)))
        except Exception as e:
            traceback.print_exc()
            self._send(502, json.dumps({"error": str(e)}))


PAGE = r"""<!doctype html>
<meta charset="utf-8"><title>BFi91XTD gateway</title>
<style>
:root{--bg:#05070f;--panel:#0c1222;--line:#1b2740;--ink:#c9d6f0;--dim:#6b7c9e;
      --accent:#00a9ce;--warn:#ffb020;--bad:#ff5a5a;--ok:#4ade80}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{display:flex;align-items:baseline;gap:16px;padding:14px 20px;
       border-bottom:1px solid var(--line)}
h1{font-size:15px;margin:0;letter-spacing:.14em;text-transform:uppercase}
.dev{color:var(--dim)}
.ro{color:var(--warn);border:1px solid var(--warn);padding:1px 7px;border-radius:3px;font-size:11px}
main{display:grid;grid-template-columns:minmax(320px,1fr) minmax(300px,420px);
     gap:18px;padding:18px 20px;align-items:start}
@media(max-width:820px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;
      padding:14px 16px;margin-bottom:16px}
.card h2{font-size:11px;letter-spacing:.16em;text-transform:uppercase;
         color:var(--dim);margin:0 0 10px}
table{width:100%;border-collapse:collapse}
td{padding:3px 0;vertical-align:top}
td:first-child{color:var(--dim);width:44%;padding-right:10px;word-break:break-all}
td:last-child{word-break:break-all}
button{background:#12203a;color:var(--ink);border:1px solid var(--line);
       border-radius:4px;padding:5px 11px;font:inherit;cursor:pointer}
button:hover{border-color:var(--accent)}
nav{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
nav button.on{border-color:var(--accent);color:#fff}
.err{color:var(--bad)}
.hint{color:#55658a;font-size:11px;line-height:1.35;margin-top:1px;
      max-width:46ch;white-space:normal}
.sky{display:block;width:100%;height:auto;background:#01030a;border-radius:6px}
.mrow{display:flex;justify-content:space-between;padding:2px 0}
.mrow span:first-child{color:var(--dim)}
.tag{font-size:11px;padding:1px 7px;border-radius:3px;border:1px solid}
.tag.ok{color:var(--ok);border-color:var(--ok)}
.tag.move{color:var(--warn);border-color:var(--warn)}
</style>
<header>
  <h1>BFi91XTD &mdash; gateway</h1>
  <span class="dev" id="dev"></span>
  <span class="ro" id="ro" hidden>read-only</span>
</header>
<main>
  <div>
    <nav id="tabs"></nav>
    <div class="card"><h2 id="title">status</h2>
      <div id="body">reading&hellip;</div>
      <div id="wr" hidden style="margin-top:12px;border-top:1px solid var(--line);padding-top:10px">
        <div class="hint" style="max-width:none">write &mdash; a JSON object with only the keys to change
          (the device does a read-modify-write); the reply is the new state</div>
        <textarea id="wjson" rows="3" style="width:100%;margin:6px 0;background:#01030a;color:var(--ink);
          border:1px solid var(--line);border-radius:4px;font:inherit;padding:6px">{}</textarea>
        <button onclick="doWrite()">write</button> <span id="wres" class="hint"></span>
      </div></div>
    <div class="card" id="fw"><h2>firmware</h2>
      <div id="slots" class="hint">reading slots&hellip;</div>
      <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">
        <label>nRF5340 image <input type="file" id="f53"></label>
        <label><input type="checkbox" id="t53" checked> test boot</label>
        <label><input type="checkbox" id="r53" checked> reset</label>
        <button onclick="dfu('5340')">upload</button>
        <button onclick="confirmImg()">confirm running</button>
      </div>
      <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">
        <label>nRF9151 image <input type="file" id="f91"></label>
        <label>target <select id="tg91"><option value="0">app</option><option value="1">modem</option></select></label>
        <button onclick="dfu('9151')">upload via recovery</button>
      </div>
      <div id="dfup" class="hint" style="margin-top:8px"></div>
    </div>
  </div>
  <div>
    <div class="card"><h2>attitude</h2>
      <svg class="sky" id="sky" viewBox="0 0 320 320"></svg>
      <div id="mnum" style="margin-top:10px"></div>
    </div>
  </div>
</main>
<script>
const $=s=>document.querySelector(s);
let cur="status", meta={};

async function api(p,o){const r=await fetch(p,o);const j=await r.json();
  if(!r.ok) throw new Error(j.error||r.status); return j;}

/* Registers speak in sentinels and enumerations. Showing a person
 * "mcs 255" or "txpower 128" is not showing them the truth -- 0xFF is
 * "adaptive" and 0x80 is "auto", and a reader who does not know that
 * reasonably concludes the page is broken. Decode at the edge; the raw
 * value stays in view beside it so nothing is hidden.
 */
const DEC={
  "radio.mcs": v=>v===255?"adaptive (auto)":"MCS "+v,
  "radio.txpower": v=>v===128?"auto":((v>127?v-256:v)+" dBm"),
  "radio.cb_period": v=>"code "+v,
  "radio.scan_ms": v=>v+" ms",
  "dect.role": v=>({0:"none",1:"leaf",2:"relay",3:"sink"}[v]??v),
  "dect.mac_role": v=>({0:"idle",1:"FT",2:"PT"}[v]??v),
  "dect.network": v=>"0x"+(v>>>0).toString(16).padStart(8,"0"),
  "dect.carrier": v=>v+" (carrier; the band row follows)",
  "dect.band": v=>v?"band "+v:"unknown / no link",
  "ble.window_left_s": v=>v?v+" s":"closed",
  "ota.image_type": v=>({0x706b1e6a:"Thingy:91 X",0x6e3538c8:"nRF9151 DK"}[v]??("0x"+(v>>>0).toString(16))),
  "ota.board": v=>({0x706b1e6a:"Thingy:91 X",0x6e3538c8:"nRF9151 DK"}[v]??("0x"+(v>>>0).toString(16))),
  "ota.build_time": v=>v?new Date(v*1000).toISOString().slice(0,16)+"Z":"none",
  "ota.running_id": v=>v?new Date(v*1000).toISOString().slice(0,16)+"Z":"none",
  "dfu91.err": v=>v?("errno "+v):"ok",
  "dect.boot_prof": v=>"0x"+(v>>>0).toString(16),
  "wifi.state": v=>({0:"idle",1:"scanning",2:"connected",3:"disconnected"}[v]??v),
  "wifi.radio": v=>({0:"auto (sink only)",1:"on",2:"off"}[v]??v),
  "wifi.sec": v=>({0:"auto",1:"open",2:"wpa2",3:"wpa3"}[v]??v),
  "wifi.band": v=>({0:"any",1:"2.4 GHz",2:"5 GHz"}[v]??v),
  "ip.family": v=>({2:"IPv6"}[v]??v),
  "geo.source": v=>({0:"unset",1:"surveyed",2:"gnss",3:"estimated",4:"derived"}[v]??v),
  "geo.lat": v=>(v/1e7).toFixed(7)+"\u00b0",
  "geo.lon": v=>(v/1e7).toFixed(7)+"\u00b0",
  "geo.alt_mm": v=>(v/1000).toFixed(3)+" m",
  "geo.h_acc_m": v=>v===255?"unknown":"\u00b1"+v+" m",
  "geo.v_acc_m": v=>v===255?"unknown":"\u00b1"+v+" m",
  "cloud.dtls": v=>v?"on (5684)":"off (5683)",
  "led.lv": v=>v+" %", "led.fl": v=>v+" %", "led.br": v=>v+" ms",
  "led.gr": v=>v+" %", "led.gg": v=>v+" %", "led.gb": v=>v+" %",
  "sys.reset_cause": v=>"0x"+(v>>>0).toString(16),
  "chipid.modem_id": v=>"0x"+(v>>>0).toString(16).padStart(8,"0"),
  "chipid.chip_id": v=>"0x"+(v>>>0).toString(16).padStart(8,"0"),
  "chipid.modem_build": v=>"0x"+(v>>>0).toString(16).padStart(8,"0"),
  "inventory.flash_jedec": v=>[(v>>16)&255,(v>>8)&255,v&255]
      .map(b=>b.toString(16).padStart(2,"0")).join(" "),
  "inventory.wifi_mac": v=>String(v).match(/../g)?.join(":")??String(v),
};

/* Every field, said in one line.
 *
 * A configuration page whose rows are bare register names is a page only
 * its author can use. These are the sentences that were otherwise only in
 * the README, the register map, or somebody's head -- including the ones
 * that matter most, like a key that cannot be read back and a plan that
 * the cloud copy overwrites.
 */
const HELP={
 "chipid.dev_id":"nRF5340 FICR DEVICEID \u2014 64 bits, unique per die, not writable. THIS is what a licence binds to",
 "chipid.dev_id_len":"bytes of device id returned; empty rather than zeros if hwinfo cannot answer",
 "chipid.modem_id":"nRF9151 Long RD ID \u2014 FICR-derived and survives chip erase, but 32-bit and the register is writable. An identity, not a root of trust",
 "chipid.modem_id_valid":"the value above was actually read (host interface up)",
 "chipid.chip_id":"9151 product magic, identical on every board \u2014 useless for identity, listed so nobody uses it",
 "chipid.modem_build":"9151 firmware build id",
 "inventory.flash_jedec":"external NOR part number \u2014 every one of this part answers the same, so asset record only",
 "inventory.wifi_mac":"nRF7002 MAC from OTP \u2014 unique but assigned, and readable by anything on the network",
 "inventory.bt_built":"is Bluetooth compiled into this image at all",
 "inventory.bme688":"environment sensor ready \u2014 'ready' means its WHO_AM_I already matched at init",
 "inventory.adxl367":"low-power accelerometer ready (WHO_AM_I matched)",
 "inventory.bmi270":"6-axis IMU ready (WHO_AM_I matched)",
 "inventory.bmm350":"magnetometer ready (WHO_AM_I matched)",
 "inventory.npm1300":"PMIC / charger ready",
 "status.ver":"gateway firmware version string",
 "status.spi_link":"is the SPI host interface to the nRF9151 answering (1 s CHIP_ID probe)",
 "status.usb_audio":"USB Audio (UAC2) interface enabled; changing it needs a reboot",
 "status.wifi.state":"Wi-Fi association state",
 "status.wifi.ssid":"network the Wi-Fi is associated to",
 "status.wifi.rssi":"received signal strength, dBm (closer to 0 is stronger)",

 "sys.ver":"application version", "sys.git":"source commit this image was built from",
 "sys.dirty":"the tree had uncommitted changes when this image was built",
 "sys.built":"build timestamp (UTC)", "sys.uptime":"seconds since the 5340 booted",
 "sys.reset_cause":"why the 5340 last reset (hwinfo bits)",

 "wifi.enabled":"credentials are stored (not the same as the radio being on)",
 "wifi.radio":"radio switch: 0 auto (on only when this gateway is the sink -- a relay/leaf comes up with Wi-Fi off), 1 always on, 2 always off; credentials are kept either way",
 "wifi.radio_allowed":"what the switch resolves to right now, given the DECT role",
 "wifi.sec":"security mode used when associating",
 "wifi.band":"band restriction; 'any' lets the driver choose",
 "wifi.channel":"channel currently in use",
 "wifi.rssi":"signal strength, dBm", "wifi.state":"association state",
 "wifi.ssid":"network the Wi-Fi is associated to right now (empty when it is not)",
 "wifi.cfg_ssid":"the stored network name -- what it will associate to",
 "wifi.ip6":"IPv6 address in use on the Wi-Fi interface (SLAAC; the kit has no IPv4)",

 "ip.family":"address family in use; the kit is IPv6-only",
 "ip.ipv4_supported":"false on every shipped image \u2014 there is no IPv4 stack",

 "adv.ps":"Wi-Fi power save", "adv.reg":"regulatory country code",

 "cloud.host":"ThingsBoard host", "cloud.port":"CoAP port (5683 plain, 5684 DTLS)",
 "cloud.interval":"seconds between gateway telemetry posts",
 "cloud.dtls":"CoAP over DTLS 1.2", "cloud.addr":"resolved server address in use",
 "cloud.configured":"a host and token are set",
 "cloud.prov_configured":"device-profile provisioning credentials are set",
 "cloud.posts":"gateway telemetry posts sent", "cloud.acked":"posts the server acknowledged",
 "cloud.failed":"posts that failed", "cloud.attr_posts":"attribute publications",
 "cloud.node_posts":"per-node telemetry posts relayed", "cloud.node_failed":"of those, failed",
 "cloud.provisioned":"nodes provisioned as their own cloud devices this boot",
 "cloud.prov_failed":"provisioning attempts that failed",
 "cloud.plan_published":"site plans published to the cloud",
 "cloud.topo_overflow":"topology string too long to send",
 "cloud.last_err":"last error code from the uplink",
 "cloud.dtls_hs":"DTLS handshakes", "cloud.dtls_hs_fail":"handshakes that failed",
 "cloud.rpc_reg":"RPC registrations", "cloud.rpc_sub":"RPC subscriptions",
 "cloud.rpc_rx":"RPC requests received", "cloud.rpc_ok":"RPC handled", "cloud.rpc_err":"RPC failed",

 "dect.network":"DECT NR+ Network ID shared by the whole mesh",
 "dect.carrier":"cluster carrier this sink beacons on",
 "dect.nw_carrier":"network-beacon carrier for cross-carrier discovery; 0 = none",
 "dect.sink":"this radio is the mesh sink",
 "dect.autostart":"the 9151 replays its boot profile on its own reset",
 "dect.band":"operating band of the stored carrier, per the 9151's own capability table (0x1F00). The 9151 has no band register: the band follows the carrier and the stack switches the modem band group itself",
 "dect.bands":"the 9151's band table: number, power class (2 = 21 dBm, 3 = 19 dBm), carrier range. Write {\"band\": N} to store that band's middle carrier, or {\"band\": N, \"carrier\": C} to check C against it; then apply (sink) or role 1 + snapshot (leaf)",
 "ble.advertising":"the BLE configuration window is open: SMP over GATT (this same config plane) and the shell over NUS. Off by default; write {\"on\": seconds} / {\"off\": true} / {\"unpair\": true}",
 "ble.name":"advertised name, BFi53-<long RD id>: one per board",
 "ble.pair_fail":"pairing failures \u2014 each one drops that bond; forget the device on the phone as well",
 "ota.slots":"the typed image store: one slot per board type. Putting an image here starts serving it to the mesh; nodes of that type pull it when it is newer than what they run, swap, and serve their own children",
 "ota.client_on":"this gateway's own 9151 may pull from ITS parent's store (a relay/leaf gateway); off on a sink",
 "ota.queries":"nodes that asked what is on offer / infos: answers sent / blocks: image blocks served",
 "dfu91.ready":"the 9151 slot holds a complete image (single-slot MCUboot: a failed upload leaves it EMPTY until the next one succeeds)",
 "hif.suspended":"the SPI host interface is parked so another host (a bench cable) can own the 9151; always expires",
 "dect.sense":"non-sink report period toward the sink, s \u2014 the host sets SENSE_DST/MODE/PERIOD on role write, snapshot and link-up; 0 = leave the 9151 alone",
 "dect.link":"the host interface is up",
 "ble.ready":"the BLE host is up (SMP over GATT + shell over NUS compiled in)",
 "ble.advertising":"a configuration window is open and no phone is connected (LED2 blue breath)",
 "ble.connected":"a phone holds the one connection",
 "ble.secured":"the link is encrypted (Just Works pairing done); SMP and the shell answer only then",
 "ble.peer":"address of the connected phone",
 "ble.window_left_s":"seconds until the window closes by itself (0 = closed)",
 "ble.opens":"windows opened since boot (USB --on or Button 1 held 3 s)",
 "ble.pair_fail":"pairings that failed or timed out (the link is dropped)",
 "dect.role":"role written into the role register",
 "dect.hop":"sink advertisement hop limit (mesh depth reachable)",
 "dect.hop_stored":"hop limit saved in the boot profile \u2014 pass this back when re-writing a role, or the mesh flattens",
 "dect.sr_hop":"hop limit currently in the SR word",
 "dect.role_carrier":"carrier bound to the role",
 "dect.ft_up":"the FT face is beaconing",
 "dect.awaiting":"waiting on an association",
 "dect.is_sink":"the radio reports itself as sink",
 "dect.mac_role":"role the MAC is actually running",
 "dect.phy_ready":"the PHY has started",
 "dect.boot_prof":"boot-profile word as stored in the 9151's NVS",

 "sec.require":"refuse associations that carry no MAC security",
 "sec.profile_has_key":"the BOOT PROFILE contains a key \u2014 not proof a key is live now",
 "sec.known":"the stack reports a key it can use",
 "sec.link":"host interface up",
 "sec.key":"128-bit master key \u2014 WRITE ONLY. No interface can read it back; what comes back is never a confirmation",

 "radio.mcs":"DCH modulation/coding; 255 (0xFF) means the stack adapts it",
 "radio.txpower":"transmit power; 128 (0x80) means the stack chooses",
 "radio.chain":"same-carrier chain mode",
 "radio.cb_period":"cluster beacon period code \u2014 association and discovery only, NOT the data grant",
 "radio.scan_ms":"scan timeout in milliseconds",

 "geo.lat":"latitude, WGS-84, north positive",
 "geo.lon":"longitude, east positive",
 "geo.alt_mm":"installation height",
 "geo.msl":"height is above mean sea level (else above the WGS-84 ellipsoid) \u2014 they differ by tens of metres",
 "geo.source":"how the position was obtained; provenance decides how much to trust it",
 "geo.h_acc_m":"horizontal accuracy; 255 means unknown",
 "geo.v_acc_m":"vertical accuracy; 255 means unknown",
 "geo.set":"has anyone ever written this \u2014 all-zero is a real place, so zeros are not 'unset'",

 "led.en":"animate the air-quality colour (off holds it steady)",
 "led.lv":"overall brightness", "led.fl":"darkest point of the breath",
 "led.br":"base breath period; the worst air-quality bands scale it down",
 "led.gr":"red channel scaling", "led.gg":"green channel scaling",
 "led.gb":"blue channel scaling \u2014 settled by eye against the real LED, not computed",

 "br.enabled":"IPv6 border router on",
 "br.prefix":"the /64 given to the mesh over CDD \u2014 this is the MESH side, not the Wi-Fi side",
 "br.mcast":"multicast groups forwarded into the mesh",
 "br.rx_pkts":"IPv6 packets received from the mesh (uplink)", "br.rx_bad":"received frames that were not a valid packet",
 "br.rx_nomem":"received packets dropped for lack of buffers", "br.tx_pkts":"packets sent into the mesh (downlink)",
 "br.tx_err":"downlink sends the radio refused", "br.tx_drop_no_ep":"downlink dropped: no endpoint registered",
 "br.tx_drop_prefix":"downlink dropped: destination outside the mesh /64",

 "motion.roll":"roll, degrees, right wing down positive", "motion.pitch":"pitch, degrees, nose up positive",
 "motion.heading":"tilt-compensated magnetic heading, degrees from north",
 "motion.amag":"acceleration magnitude in g \u2014 reads 1.00 at rest, the sanity check",
 "motion.adev":"acceleration deviation from 1 g", "motion.gmag":"rotation rate magnitude, degrees/s",
 "motion.amax":"peak acceleration since last read (hold)", "motion.gmax":"peak rotation rate since last read (hold)",
 "motion.moving":"motion detected (hysteresis: on above 0.08 g / 8 deg/s, off below 0.04 g / 4 deg/s)",

 "vpn.enabled":"WireGuard tunnel on", "vpn.up":"tunnel is established",
 "vpn.endpoint":"peer host or address", "vpn.port":"peer UDP port",
 "vpn.server_pub":"peer's public key", "vpn.my_addr":"this device's tunnel address",
 "vpn.allowed":"prefixes routed into the tunnel",
 "vpn.keepalive":"persistent keepalive, seconds",
 "vpn.my_pub":"this device's public key (known rough edge: reads empty)",

 "usb.audio":"expose the USB Audio interface", "usb.active":"USB is enumerated by a host",
 "usb.reboot_required":"a change is waiting for a reboot to take effect",

 "plan.plan":"the CDC site plan as JSON. A plan applied here stands until someone EDITS the ThingsBoard copy after it (last editor wins); the lock below refuses the cloud copy outright",
 "plan.source":"who put the plan on the air: cloud (ThingsBoard poll) or local (USB/shell)",
 "plan.lock":"plan lock \u2014 while on, the ThingsBoard copy is reported as diverged but never applied",
 "plan.diverged":"the ThingsBoard copy differs from what is on the air and was NOT applied (lock, or a newer local plan)",
 "plan.seq":"CDC sequence; publishing bumps it and every node re-adopts",
 "plan.sink_seq":"sequence the sink is actually serving",
 "plan.items":"CDC items in the published blob (122 B total budget)",
 "plan.live":"the plan is live on the radio",
 "plan.valid":"the stored plan parsed",
 "plan.received":"plans received from the cloud",
};

// The firmware still emits these IPv4 fields; they are always empty and the
// kit has no IPv4 stack, so the page does not show them at all.
const HIDE=new Set(["wifi.ip4","wifi.mask4","wifi.gw4","status.wifi.ip4",
                    "ip.mode","ip.addr","ip.mask","ip.gw"]);

function table(o){
  if(o===null||typeof o!=="object") return String(o);
  if(Array.isArray(o)) return o.map(table).join("<hr style='border:0;border-top:1px solid #1b2740'>");
  let h="<table>";
  for(const k of Object.keys(o)){
    if(HIDE.has(cur+"."+k)) continue;
    const v=o[k];
    let cell;
    if(typeof v==="object"&&v!==null){
      const save=cur; cur=cur+"."+k; cell=table(v); cur=save;
    }
    else{
      const f=DEC[cur+"."+k];
      cell = f ? `${f(v)} <span style="color:var(--dim)">(${v})</span>` : String(v);
    }
    const hp=HELP[cur+"."+k];
    h+=`<tr><td>${k}${hp?`<div class="hint">${hp}</div>`:""}</td><td>${cell}</td></tr>`;
  }
  return h+"</table>";
}

async function doWrite(){
  try{
    const body=JSON.parse($("#wjson").value||"{}");
    $("#wres").textContent="writing…";
    const r=await api("/api/write/"+cur,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    $("#wres").textContent="ok"; $("#body").innerHTML=table(r);
  }catch(e){ $("#wres").innerHTML=`<span class="err">${e.message}</span>`; }
}

function fileB64(inp){
  return new Promise((res,rej)=>{
    const f=inp.files[0]; if(!f){ rej(new Error("choose a file first")); return; }
    const rd=new FileReader(); rd.onload=()=>res(rd.result.split(",")[1]); rd.onerror=rej;
    rd.readAsDataURL(f);
  });
}

async function slots(){
  try{
    const s=await api("/api/slots");
    $("#slots").innerHTML=s.map(x=>`slot ${x.slot}  <b>${x.version}</b>  ${x.hash.slice(0,16)}  ${x.flags.join(" ")}`).join("<br>");
  }catch(e){ $("#slots").innerHTML=`<span class="err">${e.message}</span>`; }
}

async function dfu(what){
  try{
    const inp=$(what==="5340"?"#f53":"#f91");
    const image=await fileB64(inp);
    const body=what==="5340"?{image,test:$("#t53").checked,reset:$("#r53").checked}
                             :{image,target:$("#tg91").value};
    await api("/api/dfu/"+what,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const tick=setInterval(async()=>{
      const d=await api("/api/dfu");
      const pct=d.total?Math.round(100*d.off/d.total):0;
      let t=`${d.what}: ${d.off}/${d.total} B  ${pct}%  ${d.rate.toFixed(1)} KiB/s`;
      if(!d.busy){ clearInterval(tick);
        t+= d.error?` <span class="err">${d.error}</span>`:" &mdash; done "+JSON.stringify(d.result);
        slots(); }
      $("#dfup").innerHTML=t;
    },1000);
  }catch(e){ $("#dfup").innerHTML=`<span class="err">${e.message}</span>`; }
}

async function confirmImg(){
  try{ const s=await api("/api/confirm",{method:"POST",body:"{}"}); $("#dfup").textContent="confirmed"; slots(); }
  catch(e){ $("#dfup").innerHTML=`<span class="err">${e.message}</span>`; }
}

async function show(name){
  cur=name; $("#title").textContent=name;
  $("#wr").hidden = !(meta.write && meta.writable && meta.writable.includes(name));
  $("#wjson").value="{}"; $("#wres").textContent="";
  for(const b of document.querySelectorAll("#tabs button"))
    b.classList.toggle("on", b.textContent===name);
  $("#body").textContent="reading…";
  try{ $("#body").innerHTML=table(await api("/api/read/"+name)); }
  catch(e){ $("#body").innerHTML=`<span class="err">${e.message}</span>`; }
}

/* The attitude display.
 *
 * An attitude indicator is spacecraft instrumentation, so it is drawn as
 * one: a planet limb below, sky above, the horizon rolling and rising
 * with the board. Stars are fixed to the heading, so turning the box
 * sweeps them past -- that is the only way an absolute-looking heading
 * can honestly show relative change, which is all an uncalibrated indoor
 * magnetometer is good for.
 */
const STARS=[];
for(let i=0;i<90;i++) STARS.push({a:Math.random()*360, y:Math.random()*150+10,
                                  r:Math.random()*1.1+0.3, t:Math.random()});
function sky(m){
  const cx=160, cy=160, R=132;
  const roll=(m.roll||0), pitch=(m.pitch||0), head=(m.heading||0);
  const off=Math.max(-120,Math.min(120, pitch*2.2));
  let s=`<defs>
    <clipPath id="c"><circle cx="${cx}" cy="${cy}" r="${R}"/></clipPath>
    <radialGradient id="g" cx="50%" cy="100%" r="90%">
      <stop offset="0%" stop-color="#1d4a63"/><stop offset="100%" stop-color="#07182a"/>
    </radialGradient>
    <linearGradient id="limb" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#2b7fa0"/><stop offset="35%" stop-color="#123a55"/>
      <stop offset="100%" stop-color="#04101c"/>
    </linearGradient></defs>
    <circle cx="${cx}" cy="${cy}" r="${R}" fill="#01030a"/>
    <g clip-path="url(#c)"><g transform="rotate(${-roll} ${cx} ${cy}) translate(0 ${off})">
      <rect x="-200" y="-260" width="720" height="420" fill="url(#g)"/>`;
  for(const st of STARS){
    const x=cx+((st.a-head+540)%360-180)*2.6, y=cy-160+st.y;
    const tw=0.45+0.55*Math.abs(Math.sin(Date.now()/900+st.t*6));
    s+=`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${st.r.toFixed(2)}"
         fill="#dce9ff" opacity="${tw.toFixed(2)}"/>`;
  }
  s+=`<ellipse cx="${cx}" cy="${cy+250}" rx="420" ry="260" fill="url(#limb)"/>
      <path d="M-200 ${cy} H520" stroke="#7fd4ef" stroke-width="1.2" opacity=".85"/>`;
  for(let p=-60;p<=60;p+=15){ if(!p) continue;
    const y=cy-p*2.2, w=p%30?16:30;
    s+=`<path d="M${cx-w} ${y} H${cx+w}" stroke="#7fd4ef" stroke-width=".8" opacity=".5"/>`;
  }
  s+=`</g></g>
    <circle cx="${cx}" cy="${cy}" r="${R}" fill="none" stroke="#1b2740"/>
    <path d="M${cx-34} ${cy} H${cx-12} M${cx+12} ${cy} H${cx+34}
             M${cx} ${cy-7} V${cy+7}" stroke="#ffb020" stroke-width="2"/>
    <circle cx="${cx}" cy="${cy}" r="2.2" fill="#ffb020"/>`;
  /* roll pointer on the rim */
  const a=(-roll-90)*Math.PI/180;
  s+=`<circle cx="${(cx+Math.cos(a)*(R-9)).toFixed(1)}"
        cy="${(cy+Math.sin(a)*(R-9)).toFixed(1)}" r="3.4" fill="#00a9ce"/>`;
  return s;
}

async function motion(){
  try{
    const m=await api("/api/read/motion");
    const d=x=>(x/100);
    const v={roll:d(m.roll),pitch:d(m.pitch),heading:d(m.heading)};
    $("#sky").innerHTML=sky(v);
    const row=(k,val)=>`<div class="mrow"><span>${k}</span><span>${val}</span></div>`;
    $("#mnum").innerHTML=
      row("roll", v.roll.toFixed(2)+"&deg;")+
      row("pitch", v.pitch.toFixed(2)+"&deg;")+
      row("heading", m.heading_valid? v.heading.toFixed(1)+"&deg; (relative)":"&mdash;")+
      row("|a|", d(m.amag).toFixed(2)+" g")+
      row("peak &Delta;a", d(m.amax).toFixed(2)+" g")+
      row("|&omega;|", d(m.gmag).toFixed(2)+" &deg;/s")+
      row("peak &omega;", d(m.gmax).toFixed(2)+" &deg;/s")+
      `<div class="mrow"><span>state</span><span>
        <span class="tag ${m.moving?"move":"ok"}">${m.moving?"moving":"still"}</span>
        <span class="tag ${m.on_batt?"move":"ok"}">${m.on_batt?"on battery":"usb power"}</span>
       </span></div>`;
  }catch(e){ $("#mnum").innerHTML=`<span class="err">${e.message}</span>`; }
}

(async()=>{
  meta=await api("/api/meta");
  $("#dev").textContent=meta.dev;
  if(!meta.write) $("#ro").hidden=false;
  const nav=$("#tabs");
  for(const s of meta.subsys){
    if(s==="motion") continue;             /* it has its own panel */
    const b=document.createElement("button");
    b.textContent=s; b.onclick=()=>show(s); nav.appendChild(b);
  }
  show("status"); slots();
  if(!meta.write) $("#fw").querySelectorAll("button").forEach(b=>b.disabled=true);
  motion(); setInterval(motion, 1000);
  setInterval(()=>{ if(cur!=="plan") show(cur); }, 10000);
})();
</script>
"""


def main():
    global DEV, ALLOW_WRITE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-d", "--dev", default=None)
    ap.add_argument("-p", "--port", type=int, default=8765)
    ap.add_argument("--allow-write", action="store_true",
                    help="permit configuration changes from the page")
    ns = ap.parse_args()
    DEV = ns.dev or G.default_dev()
    ALLOW_WRITE = ns.allow_write

    # 127.0.0.1 only, and said out loud. This serves a device's live
    # configuration with no authentication of any kind; binding it to a
    # routable address would put the config plane on the network, which is
    # exactly the gap the specs record as still open.
    srv = ThreadingHTTPServer(("127.0.0.1", ns.port), Handler)
    print("gwui on http://127.0.0.1:%d  device %s  %s"
          % (ns.port, DEV, "read/write" if ALLOW_WRITE else "READ-ONLY"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
