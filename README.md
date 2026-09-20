# flsemi-xtd

Local tools for the **FLSEMI BFi91XTD** DECT NR+ evaluation kit — a Nordic Thingy:91 X running FLSEMI's own DECT NR+ stack (BFi91NR7DLCVG on the nRF9151) and gateway firmware (BFi53USB7IP on the nRF5340).

Everything runs on the machine with the cable or the Bluetooth radio. Nothing here talks to any FLSEMI server: every value on screen came off the wire from the device in front of you.

```
pip install flsemi-xtd          # or: uvx flsemi-xtd

xtd ui                          # local dashboard at http://127.0.0.1:8765 (read-only)
xtd ui --allow-write            # ... with configuration writes enabled
xtd cfg status                  # role, carrier, association, uptime
xtd cfg dect --help             # band / carrier / role / network ID
xtd cfg dfu  gateway.bin        # nRF5340 firmware over USB
xtd cfg dfu91 stack.bin         # nRF9151 firmware through the gateway's proxy
xtd cfg ota  stack.bin          # put an nRF9151 image in the sink's store — the mesh distributes it
xtd profile dump before.json    # whole-device settings (secrets stay write-only)
```

Transport for every command: `-d /dev/cu.usbmodemXXXX1` (the SMP CDC port; `COMn` on Windows) or `-d ble:<name or rd-id>` once the device has opened its Bluetooth window (double-press Button 2). On macOS run from Terminal.app so the Bluetooth permission prompt can appear.

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
