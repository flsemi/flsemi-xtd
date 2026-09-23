# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""flsemi — local tools for the BFi91XTD DECT NR+ evaluation kit (Nordic Thingy:91 X).

    flsemi ui        local browser dashboard, read-only unless --allow-write
    flsemi cfg ...   configuration plane over USB CDC or Bluetooth LE (-d ble:<rdid>)
    flsemi profile   dump / diff / restore a whole device
    flsemi sh        raw shell helper (hif r/w over CDC-ACM#1)

Nothing here talks to any FLSEMI server. Every value comes off the wire from the device in front of you.
"""
__version__ = "0.5.0"
