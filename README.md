# flsemi-xtd

Local tools for the **FLSEMI BFi91XTD** DECT NR+ evaluation kit — a Nordic Thingy:91 X running FLSEMI's own DECT NR+ stack (BFi91NR7DLCVG on the nRF9151) and gateway firmware (BFi53USB7IP on the nRF5340).

Everything runs on the machine with the cable or the Bluetooth radio. Nothing here talks to any FLSEMI server: every value on screen came off the wire from the device in front of you.

```
pip install flsemi-xtd          # or: uvx flsemi-xtd

xtd ui -d PORT                  # local dashboard at http://127.0.0.1:8765 (read-only)
xtd ui -d PORT --allow-write    # ... with configuration writes enabled
xtd cfg -d PORT status          # role, carrier, association, uptime
xtd cfg -d PORT dect --help     # band / carrier / role / network ID
xtd cfg -d PORT dfu   gateway.bin   # nRF5340 firmware over USB (MCUboot image management)
xtd cfg -d PORT dfu91 stack.bin     # nRF9151 firmware through the gateway's serial-recovery proxy
xtd cfg -d PORT ota   stack.bin     # put an nRF9151 image in the sink's store — the mesh distributes it
xtd profile -d PORT dump -o before.json   # whole-device settings (secrets stay write-only)
xtd sh -d SHELLPORT 'hif r 0x0024 4'      # raw nRF9151 host-interface register access
```

Transport for every command: `-d /dev/cu.usbmodemXXXX1` (the SMP CDC port; `COMn` on Windows) or `-d ble:<name or rd-id>` once the device has opened its Bluetooth window (double-press Button 2). `-d` may go before or after the subcommand. On macOS run from Terminal.app so the Bluetooth permission prompt can appear. `xtd sh` is the one command that uses the *second* CDC port (the diagnostic shell, `...XXXX3`).

## Commands

| `xtd cfg ...` | Group 64 id | What it does |
|---|---|---|
| `status` | 0 | firmware version, SPI link to the nRF9151, USB audio, Wi-Fi association |
| `wifi` / `scan` / `ip` / `adv` | 1 / 2 / 11 / 12 | credentials and radio switch, site survey, address family, power-save and country |
| `dect` | 3 | network id, carrier/band, parent pin, sink flag, autostart, report period, role, hop limit; `--apply`, `--snapshot` |
| `sec` | 5 | DECT NR+ MAC security: master key (write-only), `--require` |
| `usb` | 7 | USB Audio (UAC2) on/off |
| `dfu91` | 8 | nRF9151 image via the gateway's MCUboot serial-recovery proxy |
| `sys` | 9 | version/uptime; `--reset warm\|cold\|factory\|modem\|wifi\|wifi-creds\|modem-stack\|modem-factory` |
| `sense` | 15 | latest sample per node; `--temp-offset` |
| `cloud` | 16 | ThingsBoard CoAP uplink: host, port, token, interval, DTLS, per-node tokens, proxy exclusions |
| `hif` | 17 | park the SPI host interface so another host can own the nRF9151 |
| `led` | 18 | air-quality indicator (LED2) and the nRF9151's LED1 |
| `radio` | 19 | MCS, TX power, chain mode, cluster-beacon period, scan timeout |
| `plan` | 20 | CDC site plan (TS 103 636-5 Annex C): read, `--raw`, `--lock`, apply from file |
| `br` | 21 | IPv6 border router prefix and enable |
| `vpn` | 22 | WireGuard tunnel parameters |
| `geo` | 23 | installation position and height (stored in the nRF9151) |
| `motion` | 24 | attitude from the on-board IMU and magnetometer |
| `chipid` | 25 | FICR device ids of both processors (licence binding) |
| `inventory` | 26 | every part on the board that can say who it is |
| `ota` | 27 | node image store: upload, `--clear`, `--client`, `--apply`, `--board` |
| `ble` | 28 | Bluetooth configuration window: `--on`, `--off`, `--unpair` |
| `dfu` | group 1 | this gateway's nRF5340: upload, `--test --reset`, `--confirm` |

Every id the gateway firmware serves (0.6.2) has a command here; the dashboard (`xtd ui`) exposes the same set as panels. `xtd profile dump/diff/restore` round-trips the settable subset of all of them, and `xtd profile site` snapshots the mesh as it is (plan, who is attached to whom, health).

## Verified on hardware

Run against two BFi91XTD kits on the bench (gateway firmware 0.6.2, stack Rev 1.513), one as the mesh sink and one as a leaf on it:

- Every read command on both kits; every write command on the leaf with read-back and restore.
- `dfu` (816 KB nRF5340 image, 36 s), `dfu91` (450 KB nRF9151 image over serial recovery, 80 s, the leaf back on the tree with its profile intact), `ota` (445 KB into the store at 16 KiB/s, sha256 matching).
- All eight `sys --reset` variants, including a 5340 factory reset followed by `profile restore`, and a 9151 `modem-factory` after which the gateway re-applied its stored profile and the leaf re-associated on its own.
- `xtd sh` register reads and writes against the nRF9151 map (`CHIP_ID`, `LONG_RD_ID`, `BOOT_COUNT`/`RESET_CAUSE`, the `GEO_*` block).
- The dashboard: every `/api/read/<subsystem>` route, a write, MCUboot slot listing, live attitude.

Things the interface does not do, by design of the firmware rather than of this tool:

- `wifi` reads back the **connected** SSID, not the stored one. On a gateway that is not associated the SSID reads empty, and `profile dump` therefore cannot carry Wi-Fi credentials — supply `--psk` (and the SSID) to `restore` by hand.
- `scan` needs Wi-Fi enabled with credentials; with the radio down the gateway answers `EUNKNOWN`.
- `geo` cannot be returned to "not set": the nRF9151 marks the record written on any write. Positions can be changed, not forgotten.
- `ip --static/--dhcp` answers `EINVAL` on the shipped image, which is IPv6-only (`ip` reports `ipv4_supported: false`).
- Group 64 ids 4, 6 and 10 (DECT_STARTUP, DECT_SHELL, AUDIO_CFG) are named in the firmware but not served; there is nothing to call.

## What it talks to

| Path | Transport | Protocol |
|---|---|---|
| Gateway configuration plane | USB CDC-ACM #0 or BLE SMP GATT | SMP, custom group 64 (this tool) |
| Gateway firmware (nRF5340) | same | standard SMP image management — [nRF Connect Device Manager](https://www.nordicsemi.com/Products/Development-tools/nRF-Connect-Device-Manager) on a phone also works |
| Stack firmware (nRF9151) | same | group 64 `dfu91` (serial-recovery proxy) |
| Diagnostic shell | USB CDC-ACM #1 or BLE NUS | plain text, `hif r/w <addr> <n>` reads and writes the nRF9151 host-interface registers |

The full host-interface register map and the configuration command set are in the kit's documentation. This repository is the open half: how the host side speaks; the DECT NR+ stack itself is licensed separately.

## Regulatory note

The evaluation kit is supplied under 47 CFR §2.803(c)(2) for evaluation only and holds no FCC, RED or DECT NR+ certification. See the notice shipped with the kit.

## Licence

BSD-3-Clause. Copyright (c) 2026 BANFi Semiconductor Co., Ltd. and FL Semiconductor LLC.
Nordic Semiconductor, Thingy:91 X, nRF9151 and nRF5340 are trademarks of Nordic Semiconductor ASA.
