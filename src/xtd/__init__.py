# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""flsemi-xtd — local tools for the BFi91XTD DECT NR+ evaluation kit (Nordic Thingy:91 X).

    xtd ui        local browser dashboard, read-only unless --allow-write
    xtd cfg ...   configuration plane over USB CDC or Bluetooth LE (-d ble:<rdid>)
    xtd profile   dump / diff / restore a whole device
    xtd sh        raw shell helper (hif r/w over CDC-ACM#1)

Nothing here talks to any FLSEMI server. Every value comes off the wire from the device in front of you.
"""
__version__ = "0.1.2"
