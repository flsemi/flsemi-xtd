#!/usr/bin/env python3
# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""gwcfg.py - talk to the BFi53USB7IP SMP group 64 over USB CDC-ACM.

mcumgr CLI cannot address custom groups; this speaks raw SMP-over-serial
(the same console framing mcumgr uses: 0x06 0x09 marker + base64 + CRC16).

Usage:
  gwcfg.py [-d /dev/tty.usbmodemXXX] status
  gwcfg.py [-d ...] usb                 # read USB config
  gwcfg.py [-d ...] usb --audio on|off  # set audio mode (applies next boot)
"""

import argparse
import base64
import glob
import struct
import sys
import time

import cbor2
import serial

GROUP_GWCFG = 64
ID_STATUS = 0
ID_WIFI_CFG = 1
ID_WIFI_SCAN = 2
ID_USB_CFG = 7
ID_WIFI_IP = 11
ID_WIFI_ADV = 12
ID_SYS = 9
ID_DECT = 3
ID_DFU91 = 8
ID_SENSE = 15
ID_CLOUD = 16
ID_HIF = 17
ID_LED = 18
ID_DECT_SEC = 5
ID_DECT_RADIO = 19
ID_CDC_PLAN = 20
ID_BR = 21
ID_VPN = 22
ID_GEO = 23
ID_MOTION = 24
ID_CHIPID = 25
ID_INVENTORY = 26
ID_OTA = 27
ID_BLE = 28

OP_READ = 0
OP_WRITE = 2

MTU = 127


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


def smp_frame(op: int, group: int, cmd_id: int, payload: dict, seq: int = 0) -> bytes:
    body = cbor2.dumps(payload)
    hdr = struct.pack(">BBHHBB", op, 0, len(body), group, seq, cmd_id)
    return hdr + body


def serial_encode(pkt: bytes):
    """base64 packet framing: [len+crc prepended] chunked into 127-byte lines."""
    full = struct.pack(">H", len(pkt) + 2) + pkt + struct.pack(">H", crc16_xmodem(pkt))
    b64 = base64.b64encode(full)
    chunks = []
    first = True
    while b64:
        room = MTU - 3  # marker(2) + newline(1)
        chunks.append((b"\x06\x09" if first else b"\x04\x14") + b64[:room] + b"\n")
        b64 = b64[room:]
        first = False
    return chunks


# ---- BLE transport (0.6.0): -d ble:<name> or -d ble:<rdid> ----------------
# The same SMP frames over the SMP GATT characteristic, on a window the
# device has opened (`gwcfg ble --on` over USB, or Button 2 double press).
# macOS: run this from Terminal.app (Bluetooth privacy permission); the first
# access to the characteristic triggers Just Works pairing - click Connect.
SMP_CHAR = "da2e7828-fbce-4e01-ae9e-261174997c48"
SMP_SVC = "8d53dc1d-1db7-4cd3-868b-8a527460aa84"
_ble = {"loop": None, "client": None, "rx": None}


def _ble_loop():
    import asyncio, threading
    if _ble["loop"] is None:
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        _ble["loop"] = loop
    return _ble["loop"]


def _ble_run(coro, timeout):
    import asyncio
    return asyncio.run_coroutine_threadsafe(coro, _ble_loop()).result(timeout)


async def _ble_connect(want: str):
    import asyncio
    from bleak import BleakScanner, BleakClient
    want = want.lower()
    dev = None
    for _ in range(3):
        found = await BleakScanner.discover(timeout=5.0, return_adv=True)
        for _, (d, adv) in found.items():
            name = (adv.local_name or "").lower()
            if want in name or any(SMP_SVC in u.lower() for u in adv.service_uuids) and want == "":
                dev = d
                break
        if dev:
            break
    if dev is None:
        raise TimeoutError("no advertising BFi53 named *%s* - open a window first "
                           "(gwcfg ble --on over USB, or Button 2 double press)" % want)
    c = BleakClient(dev, timeout=20.0)
    await c.connect()
    q = asyncio.Queue()
    _ble["rx"] = q

    def on_rx(_, data):
        q.put_nowait(bytes(data))

    # pairing happens here on macOS ("insufficient encryption" -> the OS pairs);
    # retry while the user clicks Connect
    for attempt in range(30):
        try:
            await c.start_notify(SMP_CHAR, on_rx)
            break
        except Exception as e:
            if "Not connected" in str(e):
                await c.connect()
            await asyncio.sleep(1)
    else:
        raise TimeoutError("could not enable SMP notifications (pairing not accepted?)")
    _ble["client"] = c


async def _ble_xfer(pkt: bytes, timeout: float) -> bytes:
    import asyncio
    c = _ble["client"]
    q = _ble["rx"]
    while not q.empty():
        q.get_nowait()
    mtu = max(20, (c.mtu_size or 23) - 3)
    for off in range(0, len(pkt), mtu):
        await c.write_gatt_char(SMP_CHAR, pkt[off:off + mtu], response=False)
    data = b""
    while True:
        data += await asyncio.wait_for(q.get(), timeout)
        if len(data) >= 8:
            want = struct.unpack(">H", data[2:4])[0] + 8
            if len(data) >= want:
                return data[:want]


def transceive_ble(name: str, pkt: bytes, timeout: float) -> bytes:
    if _ble["client"] is None:
        _ble_run(_ble_connect(name), 120)
    return _ble_run(_ble_xfer(pkt, timeout), timeout + 5)


def transceive(dev: str, pkt: bytes, timeout: float = 3.0) -> bytes:
    if dev.startswith("ble:"):
        return transceive_ble(dev[4:], pkt, timeout)
    with serial.Serial(dev, 115200, timeout=timeout) as port:
        for chunk in serial_encode(pkt):
            port.write(chunk)
        port.flush()

        b64 = b""
        while True:
            line = port.readline()
            if not line:
                raise TimeoutError(f"no SMP response on {dev} (is that "
                                   "CDC-ACM#0? the shell port never answers)")
            if line.startswith(b"\x06\x09") or line.startswith(b"\x04\x14"):
                b64 += line[2:].strip()
                try:
                    full = base64.b64decode(b64)
                except Exception:
                    continue
                if len(full) >= 2:
                    want = struct.unpack(">H", full[:2])[0]
                    if len(full) - 2 >= want:
                        data = full[2:2 + want - 2]  # strip len prefix and CRC
                        return data
            # other lines (log noise) are ignored


def command(dev: str, op: int, cmd_id: int, payload: dict, timeout: float = 3.0) -> dict:
    rsp = transceive(dev, smp_frame(op, GROUP_GWCFG, cmd_id, payload), timeout)
    hdr_op, _, length, group, _, rid = struct.unpack(">BBHHBB", rsp[:8])
    body = cbor2.loads(rsp[8:8 + length])
    if isinstance(body, dict) and body.get("rc", 0) not in (0, None):
        raise RuntimeError(f"SMP error rc={body['rc']} ({SMP_ERR.get(body['rc'], 'unknown')})")
    return body


# mcumgr mgmt error codes, so a refusal reads as a reason and not a number
SMP_ERR = {1: "EUNKNOWN: the gateway refused it (see its shell log)", 2: "ENOMEM",
           3: "EINVAL: bad or unsupported value for this build", 4: "ETIMEOUT",
           5: "ENOENT", 6: "EBADSTATE: not possible in the gateway's current state",
           7: "EMSGSIZE", 8: "ENOTSUP: reserved id, nothing served", 9: "ECORRUPT",
           10: "EBUSY"}


def default_dev() -> str:
    # J-Link OB VCOM ports embed the probe serial (long digit runs); the
    # gateway's CDC-ACM gets a short host-assigned suffix. Prefer short names.
    cands = [d for d in glob.glob("/dev/tty.usbmodem*")
             if len(d.split("usbmodem")[1]) <= 8]
    if not cands:
        sys.exit("no gateway CDC-ACM found (plug the device-side USB, "
                 "or pass -d /dev/tty.usbmodemXXX)")
    # The gateway enumerates two (three) CDC-ACM functions and macOS names
    # them <location><interface>: 13301 = CDC-ACM#0 = SMP, 13303 = #1 = the
    # shell. Both are the same length, so a length sort left the choice to
    # glob()'s directory order - which flipped after a cold boot on
    # 2026-09-15 and sent every SMP request to the shell, where it dies
    # silently ("no SMP response" while the OS echo on the right port
    # answered fine). Pick the lowest interface number: that is SMP.
    return sorted(cands)[0]


# ---- Upload helpers shared by the CLI and gwui.py ------------------------
GROUP_OS, GROUP_IMG = 0, 1
OS_RESET, IMG_STATE, IMG_UPLOAD = 5, 0, 1


def _progress_stderr(off, total, t0):
    rate = off / max(time.time() - t0, 0.001)
    sys.stderr.write(f"\r{off}/{total} B  {rate/1024:.1f} KiB/s   ")
    sys.stderr.flush()


def img_command(dev: str, op: int, cmd_id: int, payload: dict, timeout: float = 10.0) -> dict:
    """MCUboot image management (SMP group 1) on the same transport as gwcfg."""
    rsp = transceive(dev, smp_frame(op, GROUP_IMG, cmd_id, payload), timeout)
    length = struct.unpack(">H", rsp[2:4])[0]
    return cbor2.loads(rsp[8:8 + length])


def img_slots(dev: str) -> list:
    """[{slot, version, hash(hex), flags:[...]}] for this gateway's nRF5340."""
    out = []
    for im in img_command(dev, OP_READ, IMG_STATE, {}).get("images", []):
        out.append({"slot": im["slot"], "version": im.get("version", "?"),
                    "hash": im["hash"].hex(),
                    "flags": [k for k in ("active", "confirmed", "pending", "permanent") if im.get(k)]})
    return out


def img_upload(dev: str, blob: bytes, chunk: int = 0, progress=None) -> list:
    """Upload a zephyr.signed.bin into the nRF5340's slot 1. chunk 0 = fit the
    transport: BLE packs one SMP frame across ATT notifications, so the limit
    is the device's SMP buffer (CONFIG_MCUMGR_TRANSPORT_NETBUF_SIZE 2816) minus
    header + CBOR; USB base64 lines are 127 B each. Returns the slots after."""
    import hashlib
    sha = hashlib.sha256(blob).digest()
    chunk = chunk or (1800 if dev.startswith("ble:") else 896)
    off, t0 = 0, time.time()
    while off < len(blob):
        part = blob[off:off + chunk]
        req = {"image": 0, "off": off, "data": part}
        if off == 0:
            req["len"] = len(blob)
            req["sha"] = sha
        rsp = img_command(dev, OP_WRITE, IMG_UPLOAD, req, timeout=30.0)
        if rsp.get("rc", 0):
            raise RuntimeError(f"upload refused at {off}: rc={rsp['rc']}")
        off = rsp.get("off", off + len(part))
        if progress and ((off // chunk) % 20 == 0 or off == len(blob)):
            progress(off, len(blob), t0)
    return img_slots(dev)


def img_test(dev: str, hash_hex: str) -> list:
    img_command(dev, OP_WRITE, IMG_STATE, {"hash": bytes.fromhex(hash_hex), "confirm": False})
    return img_slots(dev)


def img_confirm(dev: str) -> list:
    img_command(dev, OP_WRITE, IMG_STATE, {"confirm": True})
    return img_slots(dev)


def os_reset(dev: str):
    """Reset the nRF5340; the reply may not arrive (BLE drops with the window)."""
    try:
        transceive(dev, smp_frame(OP_WRITE, GROUP_OS, OS_RESET, {}), 5.0)
    except Exception:
        pass


def dfu91_upload(dev: str, blob: bytes, target: int = 0, chunk: int = 896, baud: int = 0,
                 progress=None) -> dict:
    """Stream an nRF9151 image through the gateway's serial-recovery proxy
    (gwcfg id 8) and apply it. target 0 = app, 1 = modem. Returns the final
    id-8 state ({acked, mismatch, err, ...})."""
    import hashlib
    digest = hashlib.sha256(blob).digest()
    # "begin" resets the 9151 and knocks until MCUboot answers, all inside
    # the SMP handler, so the reply cannot come back for several seconds.
    command(dev, OP_WRITE, ID_DFU91,
            {"begin": {"target": target, "size": len(blob), "sha256": digest, "baud": baud}},
            timeout=20.0)
    off, t0 = 0, time.time()
    while off < len(blob):
        part = blob[off:off + chunk]
        # The first chunk is the one MCUboot erases for (three attempts of
        # eight seconds at the gateway): a short client timeout here reads
        # as "gateway rebooting". It is not.
        command(dev, OP_WRITE, ID_DFU91, {"data": {"off": off, "chunk": part}},
                timeout=40.0 if off == 0 else 10.0)
        off += len(part)
        if progress and ((off // chunk) % 40 == 0 or off == len(blob)):
            progress(off, len(blob), t0)
    return command(dev, OP_WRITE, ID_DFU91, {"apply": True}, timeout=20.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--dev", default=None, help="CDC-ACM#0 port, or ble:<name or rdid> (SMP over GATT)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    usb = sub.add_parser("usb")
    usb.add_argument("--audio", choices=["on", "off"], default=None)
    wifi = sub.add_parser("wifi")
    wifi.add_argument("--ssid", default=None)
    wifi.add_argument("--psk", default="")
    wifi.add_argument("--sec", choices=["auto", "open", "wpa2", "wpa3"],
                      default="auto")
    wifi.add_argument("--band", choices=["any", "2.4", "5"], default="any")
    wifi.add_argument("--off", action="store_true")
    scan = sub.add_parser("scan")
    scan.add_argument("--wait", type=int, default=8,
                      help="seconds to wait before fetching results")
    sub.add_parser("ip", help="Wi-Fi addressing. The kit is IPv6-only: the gateway takes "
                   "its address from router advertisements, so there is nothing to set")
    adv = sub.add_parser("adv")
    adv.add_argument("--ps", choices=["on", "off"], default=None)
    adv.add_argument("--reg", default=None, help="country code, e.g. TW")
    de = sub.add_parser("dect", help="DECT NR+ radio profile and role (9151 register map)")
    de.add_argument("--network", default=None, help="network id, e.g. 0x12345678")
    de.add_argument("--carrier", type=int, default=None,
                    help="cluster carrier; must lie in one row of the 9151's band table "
                         "(`dect` prints it as bands:[...])")
    de.add_argument("--band", type=int, default=None,
                    help="operating band number (1, 2, 4, 9, 22 on the nRF9151). Alone it "
                         "stores the middle carrier of that band; with --carrier it checks "
                         "the carrier is inside the band. The 9151 has no band register - "
                         "the band follows the carrier and the stack switches the modem's "
                         "band group (1.9 GHz vs 902-928 MHz) by itself. Add --apply (sink) "
                         "or --role leaf --snapshot (leaf) to retune now.")
    de.add_argument("--nw-carrier", type=int, default=None, help="network beacon carrier, 0 = none")
    de.add_argument("--parent", default=None, metavar="RDID",
                    help="pin this non-sink to one parent (long RD id, e.g. 0x06aa8c8f; 0 = any). "
                         "Kept by the gateway and re-asserted after every 9151 boot")
    de.add_argument("--sink", choices=["on", "off"], default=None)
    de.add_argument("--autostart", choices=["on", "off"], default=None)
    de.add_argument("--sense", type=int, default=None, help="non-sink report period, s (0 = host leaves SENSE_* alone)")
    de.add_argument("--role", choices=["none", "leaf", "relay", "sink"], default=None,
                    help="write register 0x0048 now")
    de.add_argument("--role-carrier", type=int, default=0, help="carrier for --role (0 = keep)")
    de.add_argument("--hop", type=int, default=0,
                    help="sink advert hop limit (SR[7:4]) for --role sink. It travels in the "
                         "same write as the role and DEFAULTS TO 0, so re-issuing a sink role "
                         "without it flattens the mesh depth -- pass what `dect` reports as "
                         "hop_stored")
    de.add_argument("--ft-carrier", type=int, default=0, help="a relay's own FT carrier")
    de.add_argument("--apply", action="store_true", help="run the stored profile now")
    de.add_argument("--snapshot", action="store_true",
                    help="push the stored hop into the 9151's SR word and have it save its "
                         "boot profile (AUTOSTART=1), so the value survives its own resets")
    df = sub.add_parser("dfu91", help="stage an nRF9151 image in the shared external NOR")
    df.add_argument("image", nargs="?", default=None, help="file to send (omit to read state)")
    df.add_argument("--target", choices=["app", "modem"], default="app")
    df.add_argument("--chunk", type=int, default=896,
                    help="payload bytes per SMP write (896 measured at 8.0 KiB/s; 128 was 1.9)")
    df.add_argument("--baud", type=int, default=0,
                    help="recovery UART rate on the shared traces; 0 keeps the board's "
                         "current setting (460800). Both ends must match.")
    df.add_argument("--abort", action="store_true")
    hf = sub.add_parser("hif", help="park the SPI host interface so another host can own the 9151")
    hf.add_argument("--suspend", type=int, default=None, metavar="SECONDS",
                    help="park for N seconds (max 3600); 0 resumes now. "
                         "Always expires: a parked bench looks exactly like a dead radio.")
    br = sub.add_parser("br", help="IPv6 border router: the mesh /64 given to the RDs over CDD")
    br.add_argument("--prefix", default=None,
                    help="mesh /64, e.g. 2001:db8:1:2::/64 (the /64 suffix is optional). "
                         "This is the mesh's own prefix, not the Wi-Fi side's address "
                         "and a different setting.")
    br.add_argument("--enable", choices=["on", "off"], default=None)
    wifi.add_argument("--radio", choices=["auto","on","off"], default=None,
                      help="radio switch, independent of the credentials: auto = on only when this gateway is the sink (default; a relay/leaf comes up with Wi-Fi off), on = always, off = never (credentials kept)")

    vpn = sub.add_parser("vpn", help="WireGuard tunnel")
    vpn.add_argument("--endpoint", default=None, help="host or literal address of the peer")
    vpn.add_argument("--port", type=int, default=None)
    vpn.add_argument("--server-pub", default=None, help="the peer's public key (base64)")
    vpn.add_argument("--my-addr", default=None, help="tunnel address, e.g. fdcc:9151::11/64")
    vpn.add_argument("--allowed", default=None, help="comma-separated allowed IPs")
    vpn.add_argument("--keepalive", type=int, default=None, help="seconds")
    vpn.add_argument("--enable", choices=["on", "off"], default=None)

    plan = sub.add_parser("plan", help="CDC site plan (TS 103 636-5 Annex C) over USB")
    plan.add_argument("file", nargs="?", default=None,
                      help="JSON file to publish; '-' reads stdin. Omit to read the "
                           "stored plan. Publishing bumps CDC_SEQ and every node "
                           "re-adopts, so a plan that still names the old carriers will "
                           "overwrite hand-set ones.")
    plan.add_argument("--raw", action="store_true",
                      help="print just the plan JSON, for piping into a file")
    plan.add_argument("--lock", choices=["on", "off"], default=None,
                      help="plan lock: with it on, the ThingsBoard copy is reported "
                           "(plan_diverged) but never applied. Without the lock a "
                           "plan published here still outranks the cloud copy until "
                           "someone edits the cloud copy after it -- last editor wins, "
                           "not cloud-always-wins. The read shows source/lock/diverged.")

    ota = sub.add_parser("ota", help="node image store: the signed nRF9151 image the sink "
                                      "serves to the mesh over EP 0x2A3C (Rev 1.508 FOTA)")
    ota.add_argument("image", nargs="?", default=None,
                     help="zephyr.signed.bin to store (omit to read the store's state)")
    ota.add_argument("--chunk", type=int, default=896, help="payload bytes per SMP write")
    ota.add_argument("--clear", action="store_true", help="forget the stored image")
    ota.add_argument("--client", choices=["on","off"], default=None,
                     help="arm the pull-and-apply client (default off: INFO carries no board id, so "
                          "a parent can offer a DK image to a Thingy; turn on per board once you know "
                          "what the parent serves)")
    ota.add_argument("--apply", action="store_true",
                     help="apply the STORED image to this gateway's own nRF9151 (dfu91 serial "
                          "recovery; the slot is erased on the first chunk, the NOR copy is the retry)")
    ota.add_argument("--board", default=None,
                     help="this gateway's 9151 image type: thingy | dk | 0x<md5 first 32 bits> -- the "
                          "QUERY names it and only an INFO of that type is pulled (a124c653)")

    ble = sub.add_parser("ble", help="BLE configuration window: SMP over GATT (gwcfg + DFU) and the "
                                      "shell over NUS for a phone; off by default")
    ble.add_argument("--on", action="store_true", help="open (or re-arm) the window: advertise as the device name")
    ble.add_argument("--minutes", type=float, default=None, help="window length for --on (default 10)")
    ble.add_argument("--off", action="store_true", help="close the window now (drops the phone)")
    ble.add_argument("--unpair", action="store_true", help="forget every bond (then forget the device on the phone too)")

    dfu = sub.add_parser("dfu", help="update THIS gateway's nRF5340 (MCUboot image management, "
                                      "SMP group 1) over the same transport as everything else: USB "
                                      "or -d ble:<rdid>. Go mcumgr's darwin BLE upload panics on "
                                      "the MTU; this one does not")
    dfu.add_argument("image", nargs="?", default=None, help="zephyr.signed.bin (omit to list slots)")
    dfu.add_argument("--test", action="store_true", help="after the upload: mark it for a test boot")
    dfu.add_argument("--reset", action="store_true", help="...and reset (the BLE window does not survive it)")
    dfu.add_argument("--confirm", action="store_true", help="confirm the RUNNING image (no upload)")
    dfu.add_argument("--chunk", type=int, default=0, help="data bytes per SMP write (0 = fit the transport MTU)")

    sec = sub.add_parser("sec", help="DECT NR+ MAC security (Mode 1, TS 103 636-4 5.9)")
    sec.add_argument("--key", default=None,
                     help="128-bit master key as 32 hex digits. WRITE-ONLY: the register "
                          "returns zeros and no interface can read it back. What comes "
                          "back is NOT a confirmation: no register reports a live key. "
                          "Add --save to fold it into the boot profile, which is "
                          "confirmable (profile_has_key) and survives a reboot.")
    sec.add_argument("--save", action="store_true",
                     help="after installing the key, snapshot the boot profile "
                          "(AUTOSTART=1) so profile_has_key can confirm it")
    sec.add_argument("--require", choices=["on", "off"], default=None,
                     help="on = refuse associations that carry no MAC security "
                          "(Table 6.4.2.5-2 reject cause); off accepts both, the default, "
                          "since security is per association")

    radio = sub.add_parser("radio", help="DECT NR+ radio shaping (9151 registers)")
    radio.add_argument("--mcs", default=None, help="DCH MCS 0..4, or 'auto' for adaptive")
    radio.add_argument("--txpower", default=None,
                       help="dBm (signed), or 'auto' to let the stack choose")
    radio.add_argument("--chain", type=int, default=None, help="same-carrier chain mode")
    radio.add_argument("--cb-period", type=int, default=None, help="cluster beacon period code")
    radio.add_argument("--scan-ms", type=int, default=None, help="scan timeout, ms")

    led = sub.add_parser("led", help="air-quality indicator on LED2 (Bosch IAQ bands)")
    led.add_argument("--breathe", choices=["on", "off"], default=None,
                     help="off holds the band colour steady instead of animating it")
    led.add_argument("--level", type=int, default=None, help="brightness, percent")
    led.add_argument("--period", type=int, default=None,
                     help="base breath period in ms; the worst bands scale it down")
    led.add_argument("--floor", type=int, default=None,
                     help="darkest point of the breath, percent")
    led.add_argument("--dect", default=None, metavar="MODE|RRGGBB",
                     help="LED1 on the nRF9151 (Rev 1.511): auto | eco | off | identify (white, 3 s) "
                          "| RRGGBB hex = host colour; a host colour reverts to auto after 60 s")
    led.add_argument("--dect-period", type=int, default=None, metavar="MS",
                     help="breath period for a host colour, 0 = solid")
    led.add_argument("--gain", default=None, metavar="R,G,B",
                     help="per-channel scaling in percent, e.g. 55,100,100 -- settle "
                          "these by eye against the real LED, they cannot be computed")

    geo = sub.add_parser("geo", help="installation position and height (9151 NVS, "
                                     "survives reboots; never used on the air)")
    geo.add_argument("--lat", default=None,
                     help="latitude in decimal degrees, WGS-84, north positive (e.g. 25.0478)")
    geo.add_argument("--lon", default=None,
                     help="longitude in decimal degrees, east positive")
    geo.add_argument("--alt", default=None,
                     help="height in METRES; stored as millimetres. Say what it is "
                          "measured from with --msl")
    geo.add_argument("--msl", choices=["on", "off"], default=None,
                     help="on = height above mean sea level, off = above the WGS-84 "
                          "ellipsoid. The two differ by tens of metres and nothing "
                          "downstream can tell which one it was given")
    geo.add_argument("--source", choices=["unset", "surveyed", "gnss", "estimated", "derived"],
                     default=None, help="how the position was obtained")
    geo.add_argument("--h-acc", type=int, default=None, metavar="M",
                     help="horizontal accuracy in metres (255 = unknown)")
    geo.add_argument("--v-acc", type=int, default=None, metavar="M",
                     help="vertical accuracy in metres (255 = unknown)")

    sub.add_parser("chipid", help="chip identity of both processors (licence binding)")
    sub.add_parser("inventory", help="every part on the board that can say who it is")
    sub.add_parser("motion", help="attitude from the on-board IMU/magnetometer (roll, pitch, heading)")
    sen = sub.add_parser("sense", help="latest sample per node (nodes + gateway)")
    sen.add_argument("--temp-offset", default=None, metavar="K|auto",
                     help="self-heating correction for this board's BME688 in kelvin (e.g. 4.5), "
                          "or auto = 0 on a leaf/relay, 4.5 on a board with Wi-Fi on; RH is recomputed "
                          "at the corrected temperature so the dew point is preserved")
    sy = sub.add_parser("sys", help="version / uptime, or --reset warm|cold|factory")
    sy.add_argument("--reset", choices=["warm", "cold", "factory", "modem", "wifi", "wifi-creds", "modem-stack",
                              "modem-factory"], default=None)
    cl = sub.add_parser("cloud", help="ThingsBoard CoAP uplink: read state, or set host/token")
    cl.add_argument("--host", default=None)
    cl.add_argument("--port", type=int, default=None)
    cl.add_argument("--token", default=None)
    cl.add_argument("--interval", type=int, default=None)
    cl.add_argument("--dtls", choices=["on", "off"], default=None, help="CoAP over DTLS 1.2 (5684) to ThingsBoard")
    cl.add_argument("--noproxy", metavar="RDIDHEX[:0]", default=None, help="stop relaying this node (it terminates its own DTLS); RDID:0 resumes")
    cl.add_argument("--pkey", default=None, help="TB device-profile provision key")
    cl.add_argument("--psec", default=None, help="TB device-profile provision secret")
    cl.add_argument("--ntoken", action="append", default=None, metavar="RDIDHEX:TOKEN",
                    help="store one node's TB access token (repeatable); token distribution across gateways")
    args = ap.parse_args()

    dev = args.dev or default_dev()

    if args.cmd == "status":
        print(command(dev, OP_READ, ID_STATUS, {}))
    elif args.cmd == "usb":
        if args.audio is None:
            print(command(dev, OP_READ, ID_USB_CFG, {}))
        else:
            print(command(dev, OP_WRITE, ID_USB_CFG,
                          {"audio": args.audio == "on"}))
    elif args.cmd == "wifi":
        if args.radio is not None:
            command(dev, OP_WRITE, ID_WIFI_CFG, {"radio": {"auto": 0, "on": 1, "off": 2}[args.radio]})
        if args.off:
            print(command(dev, OP_WRITE, ID_WIFI_CFG, {"enable": False}))
        elif args.ssid is None:
            print(command(dev, OP_READ, ID_WIFI_CFG, {}))
        else:
            sec = {"auto": 0, "open": 1, "wpa2": 2, "wpa3": 3}[args.sec]
            band = {"any": 0, "2.4": 1, "5": 2}[args.band]
            print(command(dev, OP_WRITE, ID_WIFI_CFG,
                          {"ssid": args.ssid, "psk": args.psk,
                           "sec": sec, "band": band,
                           "enable": True}))
    elif args.cmd == "sys":
        if args.reset:
            print(command(dev, OP_WRITE, ID_SYS, {"reset": args.reset}))
        else:
            print(command(dev, OP_READ, ID_SYS, {}))
    elif args.cmd == "cloud" and args.noproxy:
        rd, _, flag = args.noproxy.partition(":")
        print(command(dev, OP_WRITE, ID_CLOUD, {"noproxy": int(rd, 16), "noproxy_on": 0 if flag == "0" else 1}))
    elif args.cmd == "cloud" and args.ntoken:
        for nt in args.ntoken:
            rd, tok = nt.split(":", 1)
            print(command(dev, OP_WRITE, ID_CLOUD, {"nrdid": int(rd, 16), "ntok": tok}))
    elif args.cmd == "cloud":
        if all(v is None for v in (args.host, args.port, args.interval, args.token,
                                   args.pkey, args.psec, args.dtls)):
            print(command(dev, OP_READ, ID_CLOUD, {}))
        else:
            # the gateway read-modify-writes: only send what was asked for
            req = {}
            if args.port is not None:
                req["port"] = args.port
            if args.interval is not None:
                req["interval"] = args.interval
            if args.host:
                req["host"] = args.host
            if args.token:
                req["token"] = args.token
            if args.pkey:
                req["pkey"] = args.pkey
            if args.psec:
                req["psec"] = args.psec
            if args.dtls:
                req["dtls"] = 1 if args.dtls == "on" else 0
            print(command(dev, OP_WRITE, ID_CLOUD, req))
    elif args.cmd == "br":
        req = {}
        if args.prefix is not None:
            req["prefix"] = args.prefix
        if args.enable is not None:
            req["enabled"] = args.enable == "on"
        print(command(dev, OP_READ if not req else OP_WRITE, ID_BR, req))
    elif args.cmd == "vpn":
        req = {}
        for cli, key in (("endpoint", "endpoint"), ("port", "port"),
                         ("server_pub", "server_pub"), ("my_addr", "my_addr"),
                         ("allowed", "allowed"), ("keepalive", "keepalive")):
            v = getattr(args, cli, None)
            if v is not None:
                req[key] = v
        if args.enable is not None:
            req["enabled"] = args.enable == "on"
        # my_pub comes back on every read: it is the half a peer needs, and the
        # private key is neither readable nor settable over this interface.
        print(command(dev, OP_READ if not req else OP_WRITE, ID_VPN, req))
    elif args.cmd == "plan":
        if args.lock is not None:
            command(dev, OP_WRITE, ID_CDC_PLAN, {"lock": args.lock == "on"})
        if args.file is None:
            r = command(dev, OP_READ, ID_CDC_PLAN, {})
            if args.raw:
                print(r.get("plan", ""))
            else:
                print(r)
        else:
            src = sys.stdin.read() if args.file == "-" else open(args.file).read()
            # Compact it: the 9151 stores the plan verbatim and the frame is
            # finite, so whitespace is wasted budget.
            import json as _json
            body = _json.dumps(_json.loads(src), separators=(",", ":"))
            print(command(dev, OP_WRITE, ID_CDC_PLAN, {"plan": body}))
    elif args.cmd == "sec":
        req = {}
        if args.key is not None:
            k = args.key.strip().replace(" ", "")
            if len(k) != 32:
                sys.exit("--key wants 32 hex digits (a 128-bit key)")
            try:
                req["key"] = bytes.fromhex(k)
            except ValueError:
                sys.exit("--key is not hex")
        if args.require is not None:
            req["require"] = args.require == "on"
        if args.save:
            req["save"] = True
        print(command(dev, OP_READ if not req else OP_WRITE, ID_DECT_SEC, req))
    elif args.cmd == "radio":
        req = {}
        if args.mcs is not None:
            req["mcs"] = 0xFF if args.mcs == "auto" else int(args.mcs, 0)
        if args.txpower is not None:
            # 0x80 is the register's "auto"; a signed dBm rides in [7:0].
            req["txpower"] = 0x80 if args.txpower == "auto" else (int(args.txpower, 0) & 0xFF)
        if args.chain is not None:
            req["chain"] = args.chain
        if args.cb_period is not None:
            req["cb_period"] = args.cb_period
        if args.scan_ms is not None:
            req["scan_ms"] = args.scan_ms
        r = command(dev, OP_READ if not req else OP_WRITE, ID_DECT_RADIO, req)
        print(r)
        tx = r.get("txpower", 0x80)
        print("mcs %s, txpower %s" % ("auto" if r.get("mcs") == 0xFF else r.get("mcs"),
              "auto" if tx == 0x80 else "%d dBm" % (tx - 256 if tx > 127 else tx)))
    elif args.cmd == "led":
        req = {}
        if args.breathe is not None:
            req["en"] = args.breathe == "on"
        if args.level is not None:
            req["lv"] = args.level
        if args.period is not None:
            req["br"] = args.period
        if args.floor is not None:
            req["fl"] = args.floor
        if args.gain is not None:
            try:
                r, g, b = (int(x) for x in args.gain.split(","))
            except ValueError:
                sys.exit("--gain wants three percentages, e.g. 55,100,100")
            req.update({"gr": r, "gg": g, "gb": b})
        if args.dect is not None:
            modes = {"auto": 0, "eco": 2, "off": 3}
            if args.dect in modes:
                req["dect_mode"] = modes[args.dect]
            elif args.dect == "identify":
                req.update({"dect_mode": 1, "dect_rgb": 0xFFFFFF, "dect_period": 300})
            else:
                req.update({"dect_mode": 1, "dect_rgb": int(args.dect, 16)})
            if args.dect_period is not None:
                req["dect_period"] = args.dect_period
        # An empty request reads; a partial one is a read-modify-write on the
        # device, so setting one field never clears the others.
        r = command(dev, OP_READ if not req else OP_WRITE, ID_LED, req)
        if "dect_rgb" in r:
            r["dect_rgb"] = "%06x" % r["dect_rgb"]
        print(r)
    elif args.cmd == "dect":
        req = {}
        if args.network is not None:
            req["network"] = int(args.network, 0)
        if args.carrier is not None:
            req["carrier"] = args.carrier
        if args.band is not None:
            req["band"] = args.band
        if args.nw_carrier is not None:
            req["nw_carrier"] = args.nw_carrier
        if args.parent is not None:
            req["parent"] = int(args.parent, 0)
        if args.sink is not None:
            req["sink"] = args.sink == "on"
        if args.hop is not None:
            req["hop"] = args.hop
        if args.autostart is not None:
            req["autostart"] = args.autostart == "on"
        if args.sense is not None:
            req["sense"] = args.sense
        if args.role is not None:
            req["role"] = {"none": 0, "leaf": 1, "relay": 2, "sink": 3}[args.role]
            req["role_carrier"] = args.role_carrier
            req["hop"] = args.hop
            req["ft_carrier"] = args.ft_carrier
        if args.apply:
            req["apply"] = True
        if args.snapshot:
            req["snapshot"] = True
        print(command(dev, OP_WRITE if req else OP_READ, ID_DECT, req))
    elif args.cmd == "dfu91":
        import hashlib
        if args.abort:
            print(command(dev, OP_WRITE, ID_DFU91, {"abort": True}))
        elif args.image is None:
            print(command(dev, OP_READ, ID_DFU91, {}))
        else:
            blob = open(args.image, "rb").read()
            tgt = {"app": 0, "modem": 1}[args.target]
            print(dfu91_upload(dev, blob, tgt, args.chunk, args.baud, _progress_stderr))
            sys.stderr.write("\n")
    elif args.cmd == "ota":
        import hashlib
        if args.client is not None:
            command(dev, OP_WRITE, ID_OTA, {"client": args.client == "on"})
        if args.board is not None:
            board = {"thingy": 0x706b1e6a, "dk": 0x6e3538c8}.get(args.board) or int(args.board, 0)
            command(dev, OP_WRITE, ID_OTA, {"board": board})
        if args.clear:
            print(command(dev, OP_WRITE, ID_OTA, {"clear": True}))
        elif args.apply:
            print(command(dev, OP_WRITE, ID_OTA, {"apply": True}, timeout=20.0))
        elif args.image is None:
            print(command(dev, OP_READ, ID_OTA, {}))
        else:
            blob = open(args.image, "rb").read()
            digest = hashlib.sha256(blob).digest()
            i = blob.find(b"nBFIOOTP")
            if i < 0:
                sys.exit("refused: no build stamp (nBFI/OOTP) in this image -- the store "
                         "would have no id to offer; build with cmake/build_id.cmake")
            m0, m1, bt, bid, fl, ty, chk = struct.unpack_from("<7I", blob, i)
            if chk != (m0 ^ bt ^ bid ^ fl ^ ty) or ty == 0:
                sys.exit("refused: the stamp is untyped (pre-a124c653) -- the store cannot "
                         "match it to a board; rebuild the 9151 image")
            sys.stderr.write(f"stamp: type 0x{ty:08x} build time {bt} build id {bid:08x}"
                             f"{' +dirty' if fl & 1 else ''}\n")
            print(command(dev, OP_WRITE, ID_OTA,
                          {"begin": {"size": len(blob), "sha256": digest}}, timeout=20.0))
            off = 0
            t0 = time.time()
            while off < len(blob):
                part = blob[off:off + args.chunk]
                # sector erase happens ahead of the write inside the handler
                command(dev, OP_WRITE, ID_OTA, {"data": {"off": off, "chunk": part}},
                        timeout=15.0)
                off += len(part)
                if (off // args.chunk) % 40 == 0 or off == len(blob):
                    rate = off / max(time.time() - t0, 0.001)
                    sys.stderr.write(f"\r{off}/{len(blob)} B  {rate/1024:.1f} KiB/s   ")
                    sys.stderr.flush()
            sys.stderr.write("\n")
            # finish hashes the whole image from the NOR and hunts the stamp
            print(command(dev, OP_WRITE, ID_OTA, {"finish": True}, timeout=60.0))
    elif args.cmd == "dfu":
        def show(slots):
            for sl in slots:
                print(f"slot {sl['slot']}  {sl['version']:8}  {sl['hash'][:16]}  {' '.join(sl['flags'])}")

        if args.confirm:
            show(img_confirm(dev))
        elif args.image is None:
            show(img_slots(dev))
        else:
            blob = open(args.image, "rb").read()
            t0 = time.time()
            slots = img_upload(dev, blob, args.chunk, _progress_stderr)
            sys.stderr.write(f"\ndone in {time.time() - t0:.0f} s\n")
            show(slots)
            new = [sl for sl in slots if sl["slot"] == 1]
            run = [sl for sl in slots if sl["slot"] == 0]
            if new and run and new[0]["hash"] == run[0]["hash"]:
                # MCUboot would skip the swap anyway; say so rather than
                # let "marked for test boot" imply a change that never comes
                print("same image as the one running; nothing to swap")
                args.test = args.reset = False
            if args.test:
                if not new:
                    sys.exit("no image in slot 1 after the upload")
                show(img_test(dev, new[0]["hash"]))
                print("marked for test boot")
            if args.reset:
                os_reset(dev)
                print("reset sent; confirm with `dfu --confirm` once it is back (over USB, or a new window)")
    elif args.cmd == "ble":
        if args.unpair:
            print(command(dev, OP_WRITE, ID_BLE, {"unpair": True}))
        elif args.off:
            print(command(dev, OP_WRITE, ID_BLE, {"off": True}))
        elif args.on:
            secs = int(args.minutes * 60) if args.minutes else 0
            print(command(dev, OP_WRITE, ID_BLE, {"on": secs}))
        else:
            print(command(dev, OP_READ, ID_BLE, {}))
    elif args.cmd == "hif":
        if args.suspend is None:
            print(command(dev, OP_READ, ID_HIF, {}))
        else:
            print(command(dev, OP_WRITE, ID_HIF, {"suspend": args.suspend}, timeout=10.0))
    elif args.cmd == "geo":
        req = {}
        # 1e-7 degrees, the register's unit: about 11 mm at the equator, so
        # the rounding is far below any accuracy this will ever be given.
        if args.lat is not None:
            req["lat"] = int(round(float(args.lat) * 1e7))
        if args.lon is not None:
            req["lon"] = int(round(float(args.lon) * 1e7))
        if args.alt is not None:
            req["alt_mm"] = int(round(float(args.alt) * 1000))
        if args.msl is not None:
            req["msl"] = args.msl == "on"
        if args.source is not None:
            req["source"] = {"unset": 0, "surveyed": 1, "gnss": 2,
                             "estimated": 3, "derived": 4}[args.source]
        if args.h_acc is not None:
            req["h_acc_m"] = args.h_acc
        if args.v_acc is not None:
            req["v_acc_m"] = args.v_acc
        r = command(dev, OP_READ if not req else OP_WRITE, ID_GEO, req)
        if isinstance(r, dict) and "lat" in r:
            # Echo it back the way a person wrote it, not in 1e-7 degrees:
            # a commissioning record that reads 250478000 invites someone to
            # "fix" it into a degree value on the next pass.
            src = {0: "unset", 1: "surveyed", 2: "gnss", 3: "estimated", 4: "derived"}
            print("%s  lat %.7f  lon %.7f  alt %.3f m %s | %s, h +/-%s m, v +/-%s m"
                  % ("set" if r.get("set") else "NOT SET",
                     r["lat"] / 1e7, r["lon"] / 1e7, r["alt_mm"] / 1000.0,
                     "MSL" if r.get("msl") else "ellipsoid",
                     src.get(r.get("source"), r.get("source")),
                     r.get("h_acc_m"), r.get("v_acc_m")))
        else:
            print(r)
    elif args.cmd == "chipid":
        r = command(dev, OP_READ, ID_CHIPID, {})
        d = r.get("dev_id", b"")
        print("nRF5340 dev_id  %s  (%d B, FICR DEVICEID -- unique, not writable)"
              % (d.hex() if d else "<unavailable>", r.get("dev_id_len", 0)))
        print("nRF9151 modem_id 0x%08x  %s"
              % (r.get("modem_id", 0),
                 "(Long RD ID: FICR-derived and survives chip erase, but 32-bit "
                 "and the register is writable)" if r.get("modem_id_valid")
                 else "(NOT READ -- host interface down)"))
        md = r.get("modem_dev_id", b"")
        print("nRF9151 dev_id   %s"
              % (md.hex() + "  (8 B, its own FICR DEVICEID -- unique, not writable; "
                            "the Long RD ID above is derived from its low 4 bytes)"
                 if r.get("modem_dev_id_valid")
                 else "<not available: 9151 firmware predates Rev 1.503>"))
        print("nRF9151 chip_id  0x%08x  (product magic, identical on every board)"
              % r.get("chip_id", 0))
        print("nRF9151 build    0x%08x" % r.get("modem_build", 0))
    elif args.cmd == "motion":
        print(command(dev, OP_READ, ID_MOTION, {}))
    elif args.cmd == "inventory":
        r = command(dev, OP_READ, ID_INVENTORY, {})
        if "flash_jedec" in r:
            j = r["flash_jedec"]
            print("ext flash JEDEC  %02x %02x %02x   (part number, NOT unique -- "
                  "every one of this part answers the same)"
                  % (j >> 16, (j >> 8) & 0xFF, j & 0xFF))
        mac = r.get("wifi_mac", b"")
        print("Wi-Fi MAC        %s   (nRF7002 OTP, unique per interface)"
              % (":".join("%02x" % b for b in mac) if mac else "<not up>"))
        print("Bluetooth        %s"
              % ("built; read its address with the BLE tooling" if r.get("bt_built")
                 else "NOT in this image (BLE off by default) -- no address exists"))
        parts = [(k, v) for k, v in r.items()
                 if k in ("bme688", "adxl367", "bmi270", "bmm350", "npm1300")]
        if parts:
            print("sensors          (ready == the driver already matched the part's "
                  "WHO_AM_I at init)")
            for k, v in parts:
                print("  %-10s %s" % (k, "ready" if v else "NOT READY"))
    elif args.cmd == "sense":
        if args.temp_offset is not None:
            ck = -1 if args.temp_offset == "auto" else int(round(float(args.temp_offset) * 100))
            r = command(dev, OP_WRITE, ID_SENSE, {"toff": ck})
            print({k: r[k] for k in ("toff", "toff_eff")})
        else:
            print(command(dev, OP_READ, ID_SENSE, {}))
    elif args.cmd == "scan":
        print(command(dev, OP_WRITE, ID_WIFI_SCAN, {}))
        time.sleep(args.wait)
        rsp = command(dev, OP_READ, ID_WIFI_SCAN, {})
        secs = {0: "auto", 1: "open", 2: "wpa2", 3: "wpa3"}
        bands = {1: "2.4G", 2: "5G"}
        for ap in sorted(rsp.get("aps", []), key=lambda a: -a["rssi"]):
            print(f'{ap["rssi"]:>4} dBm  ch{ap["ch"]:>3} {bands.get(ap["band"], "?"):>4}  '
                  f'{secs.get(ap["sec"], "?"):>5}  {ap["ssid"]}')
        if rsp.get("scanning"):
            print("(scan still running -- rerun with a longer --wait)")
    elif args.cmd == "ip":
        r = command(dev, OP_READ, ID_WIFI_IP, {})
        print(r)
        if not r.get("ipv4_supported", False):
            print("IPv6 only (SLAAC over Wi-Fi); no addressing to configure")
    elif args.cmd == "adv":
        if args.ps is None and args.reg is None:
            print(command(dev, OP_READ, ID_WIFI_ADV, {}))
        else:
            req = {}
            if args.ps is not None:
                req["ps"] = args.ps == "on"
            if args.reg is not None:
                req["reg"] = args.reg.upper()
            print(command(dev, OP_WRITE, ID_WIFI_ADV, req))


if __name__ == "__main__":
    main()
