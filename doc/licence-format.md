# Device licence — format and contract

A licence says: *this firmware may run on this chip.* It is signed by FLSEMI
over the chip's own FICR DEVICEID, and verified on the chip against a public
key built into the secure image.

**Nothing secret lives in the device.** Extracting a unit yields a public key,
which is of no use on another unit; forging a licence needs the private key,
which never leaves our signing machine. That is the whole reason this is the
primary mechanism rather than image encryption, whose private key would have to
sit in every unit we ship.

The format is published on purpose. A scheme that needs its structure kept
secret is not a scheme.

## What it binds to

The **FICR DEVICEID**: 8 bytes, unique per die, **not writable**, served as
`DEVICE_ID` at register `0x1FF0` on the nRF9151 and in the `chipid` read on the
configuration plane. The Long RD ID (`0x0024`) is host-writable and must never
be the anchor — the code comment at that register already says so.

A BFi91XTD carries two dies. The licence binds the **nRF9151**, which is where
the stack runs. A gateway-only licence for the nRF5340 uses the same format
with `chip = 2`.

## Layout — 112 octets, big-endian

| offset | size | field | |
|---|---|---|---|
| 0 | 4 | `magic` | `0x464C4943` (`FLIC`) |
| 4 | 1 | `version` | 1 |
| 5 | 1 | `chip` | 1 = nRF9151, 2 = nRF5340 |
| 6 | 2 | `flags` | bit 0 = evaluation unit, bit 1 = feature set reserved |
| 8 | 8 | `device_id` | FICR DEVICEID, as the chip reports it |
| 16 | 4 | `product_line` | the `image_type` this licence covers, 0 = any |
| 20 | 4 | `issued` | seconds since 1970 |
| 24 | 4 | `expires` | seconds since 1970, **0 = perpetual** |
| 28 | 16 | `order` | ASCII order or customer reference, zero padded |
| 44 | 4 | `reserved` | zero |
| 48 | 64 | `signature` | ECDSA P-256 over octets 0..47, `r‖s`, SHA-256 |

48 octets signed, 64 of signature. The signed part carries the device id, so a
licence copied to another unit fails on the comparison, not on the signature —
and the failure is diagnosable rather than mysterious.

`order` is what makes a leak traceable: every licence we issue is attributable
to the order it went out on, and that is often worth more than the cryptography.

## Verification, on the chip

1. `magic` and `version` match, otherwise reject.
2. ECDSA P-256 / SHA-256 over octets 0..47 against the built-in public key.
3. `device_id` equals this die's FICR DEVICEID.
4. `product_line` is 0 or equals this image's `image_type`.
5. `expires` is 0, or the device's notion of time is before it.

The **secure image** is where this belongs: the non-secure side is where a
modified application would run, and a check it can rewrite is not a check. PSA
crypto is already in the product build (`PSA_WANT_ALG_ECDSA`,
`PSA_WANT_ECC_SECP_R1_256`, `PSA_WANT_ALG_SHA_256`, `TFM_PARTITION_CRYPTO=y`),
so the verification needs no new dependency — the cost is the public key, the
check, and a TF-M reconfiguration.

Whether it fits is a measurement, not an assumption: the nRF9151's TF-M was
cut to 40 KB and the product image sits near 87 % of RAM. If the increment does
not fit, the fallback is the check in the non-secure application, and the
fallback's weakness has to be written down rather than glossed: it then rests
entirely on P1 — an application that could rewrite the check cannot be signed
by us, so it cannot boot on a unit whose bootloader holds our key. That is a
real boundary, and a thinner one. The decision follows the measurement.

## What an unlicensed unit does

It **boots, and says so.** No radio role starts — no association, no beacon —
and the configuration plane answers normally with `licensed: false` and a
reason. It is not bricked and not silent: a unit that fails mysteriously
generates a support case, and a unit that cannot be talked to cannot be
licensed afterwards.

## Time

`expires` needs a clock the device trusts. The nRF9151 has no RTC battery, so a
perpetual licence (`expires = 0`) is the only kind that is sound without one.
For evaluation units, the honest implementation is a **monotonic counter of
powered hours in NVS**, not wall-clock time, and the licence carries a duration
rather than a date. Until that exists, evaluation kits get a perpetual licence
and the limit lives in the contract.

## Where it is stored

Settings/NVS, key `flic`. It survives an application update and does not
survive a full erase — which is the right way round: a wiped unit needs a new
licence, and only we can issue one, for a device id we can check against what
we shipped.

## The transition, which has to be planned before the check lands

Every board on the bench today is unlicensed. Landing an enforcing check
without this step stops the whole bench at once:

1. Issue licences for every bench and demo unit first — the device ids are
   already recorded per board.
2. Ship the check **reporting only** for one release: `licensed: false` is
   visible on the configuration plane and in the logs, nothing is refused.
3. Turn on enforcement in the following release, once the estate reads
   `licensed: true` everywhere.

## Keys

The device holds **two** public keys, current and standby, and accepts a
licence that either one verifies. This costs 64 octets and one extra
verification, and it is the only thing that makes rotation possible without
returning units: a unit shipped trusting one key trusts that key for ever.
An all-zero slot is skipped. **Decide this before the first production
image** — it cannot be added to units already in the field.

The development key, for bring-up on bench hardware only:

```
fingerprint 47cccd5e61a4818144a8baf8b8d50335
6efdec79d7361edf82302c61b85f5c87d0329aa14bb40dee83d34ac411d5bdec
522909d4d37d03aa47d7f64c3d9bee659431145aada624b657b360d35ac43bcc
```

It was generated on a networked laptop inside an agent session. What
disqualifies it from production is where it was made, not what it is: a key
whose value equals a product line has to be generated on the machine that will
keep it. Units flashed with this public key are development hardware.

## Byte order

`device_id` is the two FICR words concatenated as `chipid` prints them,
`0x1FF0` then `0x1FF4`. The invariant that catches the mistake for free, and
that belongs in the firmware selftest:

> the last four octets of `device_id` equal that node's long RD id

Verified on all seven bench boards. This matters because the same value is
rendered both ways in different places — a register hexdump shows the bytes as
they sit in memory, and the RD id appears everywhere else as a number.

## Host side

`xtd licence` reads the status, `xtd licence --request` produces the request to
send us (it carries the device ids and the image type, nothing secret), and
`xtd licence install <file>` writes the blob. The tool never signs: issuing is
a process on our side, on the machine that holds the key.

**Firmware contract** — two tiers, because the licence belongs to the nRF9151
and the configuration plane belongs to the nRF5340:

- **nRF9151, host-interface registers** — the blob is written here and the
  status is read here. This is the only verifier: one blob, one implementation
  of the check, whichever way the bytes arrived.
- **nRF5340, `gwcfg` id 29 `LICENCE`** — a forwarding layer, nothing more.
  - read → the fields it read from the nRF9151:
    `{licensed, chip, device_id, product_line, issued, expires, order, reason}`
  - write → `{blob: <112 octets>}` passed down verbatim, answering the same
    read, or `EINVAL` with a reason for a malformed blob, a bad signature or
    the wrong device id.

A board with no nRF5340 (the DK, the nRF9131 EK — bench hardware, not a
product) takes the same 112 octets through the same nRF9151 registers, written
by the bench tooling. Same blob, same verifier, no second format.

Producing a request needs no new interface: `DEVICE_ID` at `0x1FF0` (8 octets,
FICR, read-only, Rev 1.503) already serves what the request carries.
