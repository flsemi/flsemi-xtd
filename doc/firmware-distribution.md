# Firmware distribution from GitHub, bound to hardware we sold

Decided 2026-09-22, revised the same day: **P3 first**. The licence bound to
each chip's FICR device id is the primary mechanism, because it is the only one
of the three with no shared secret in the shipped hardware. P1 (our own signing
key) goes with it. P2 (image encryption, one key per product line) is deferred:
it protects the binary from being read, which is a different goal from stopping
it running on hardware we did not sell, and its private key would have to live
in every unit.
This is the contract between the tool, the two firmware projects and the
manufacturing process. It is a design note, not a shipped feature.

## The two properties, kept apart

| | what it stops | mechanism | state today |
|---|---|---|---|
| **P1** | someone else's firmware running on our hardware | MCUboot image signature with **our own key**, plus APPROTECT | **absent** — both bootloaders use the MCUboot/NCS default development key (no `CONFIG_BOOT_SIGNATURE_KEY_FILE` in either project) |
| **P2** | our firmware running on hardware we did not sell | MCUboot **encrypted images**, key held only by devices we provisioned | absent — `CONFIG_BOOT_ENCRYPT_IMAGE=n` |
| **P3** | our firmware running on a unit we did not licence | licence blob signed over the FICR DEVICEID, checked in the secure image | **format fixed, host side done** (doc/licence-format.md, `flsemi licence`); the on-chip check is the firmware work |

**Why P3 and not P2 as the primary.** P2's private key sits in every unit we
ship: one extraction ends the protection for the whole product line,
retroactively. P3's device-side material is a *public* key — extracting a unit
yields nothing usable on another unit, and forging a licence needs a private key
that never leaves our signing machine. Same engineering effort, and one of the
two has no shared-secret failure mode.

What P3 does not do: someone who already holds a plaintext image *and* controls
the bootloader on their own board can patch the check out. That is what P1 and
P2 are for — P1 so our own units refuse anything we did not sign, P2 (if it is
ever done) so the plaintext is not available to begin with. P3 stops the case
that actually matters here: our published image flashed as-is onto hardware we
did not supply.

A signature gives P1 only. It is routinely mistaken for P2, and it is not.

## Why "an encoded hex on GitHub" is not a mechanism

If the customer's updater can turn the published file into something the
chip will run, the key is in the updater, and the updater is in the
customer's hands. Obfuscation changes who bothers, not who can.

The only arrangement that survives publication is: **the ciphertext is
public, the key is in the device, and nothing on the host ever holds the
plaintext.** MCUboot decrypts in the bootloader, on-chip, during the swap.
`flsemi` moves bytes it cannot read.

That is what makes a public GitHub release acceptable: to anyone without
one of our boards, the artifact is inert.

## Why one key per product line

The mesh distributes one image between nodes (`nrota`, EP 0x2A3C). Per-device
keys would mean a distinct ciphertext per node, which breaks mesh OTA outright
and does not fit in a public release. Per-batch keys keep mesh OTA working
within a batch but need a batch id carried in the image stamp, compared in the
OTA exchange and reported on the configuration plane — otherwise a mixed-batch
site produces download-then-fail loops that look like a radio problem.

One key per product line avoids all of that. A product line already has an
identity in the image: `image_type` in the stamp (`nrstamp.h`) is the board
target and layout, which is what tells a Thingy:91 X image from an nRF9151 DK
image and what stops the wrong one being installed today. The release keys on
the same field, so:

- **no new firmware field is needed** — no batch id in the stamp, none on the
  configuration plane, no change to the OTA QUERY/INFO comparison;
- mesh OTA is untouched: every node of a product line takes the same ciphertext;
- one artifact per chip per release.

**Consequence for manufacturing:** MCUboot holds the image-encryption private
key, so the bootloader is product-line-specific and is flashed at manufacture.
The key never leaves the signing machine and the devices.

**The cost, stated plainly:** one key extracted from any single unit ends the
P2 protection for that whole product line, retroactively and for every unit
ever shipped on it. Two things follow, and neither is optional:

1. **P3 carries the weight now.** With one shared key, the licence blob bound
   to the FICR DEVICEID is the only property that is per unit, and the only
   thing still standing if the line key leaks. Build it with that in mind
   rather than as a nicety.
2. **A rotation plan before the first release.** A new product line, or a
   hardware revision that gets its own bootloader, is the natural rotation
   point. Decide in advance what a leak response looks like — a new key can
   only protect units flashed after it, so the answer is a product decision,
   not an engineering one.

## What each side owns

**BFi53USB7IP (nRF5340)**
1. Own signing key (`CONFIG_BOOT_SIGNATURE_KEY_FILE`), replacing the default.
2. `CONFIG_BOOT_ENCRYPT_IMAGE=y` with the product line's KEK.
3. APPROTECT on production units.

**BFi91NR7DLCVG (nRF9151)**
1. Own signing key for the MCUboot used by the `dfu91` serial-recovery path.
2. `CONFIG_BOOT_ENCRYPT_IMAGE=y`, the same product-line KEK scheme.
3. P3, and it matters more under one shared key than it would under a narrower one:
   a licence blob bound to FICR DEVICEID (0x1FF0), checked in the secure image
   rather than the non-secure application.

**Manufacturing / business**
- Who holds the private keys, and on what machine. An offline machine is the
  minimum; an HSM is the honest answer for a key that gates an entire product
  line — under this decision, that is exactly what it does.
- The rotation point and the leak response, decided before the first release.

**flsemi (this repository)**
- `flsemi update`: read the device, fetch the manifest, pick the artifact that
  matches, verify its digest, upload it through the existing paths. It never
  decrypts, so it is finished before the firmware work starts and does not
  change when that work lands.

## Manifest

One JSON file per release, published beside the artifacts.

```json
{
  "schema": 1,
  "product": "BFi91XTD",
  "released": "2026-10-01",
  "artifacts": [
    {
      "chip": "nrf5340",
      "image_type": "0x706b1e6a",
      "version": "0.6.3",
      "file": "BFi53USB7IP-0.6.3.enc.bin",
      "sha256": "…",
      "size": 816192,
      "encrypted": true
    },
    {
      "chip": "nrf9151",
      "image_type": "0x706b1e6a",
      "version": "1.520",
      "file": "BFi91NR7DLCVG-1.520.enc.bin",
      "sha256": "…",
      "size": 450188,
      "encrypted": true
    }
  ]
}
```

`image_type` is the stamp field that already distinguishes a Thingy:91 X image
from an nRF9151 DK image, so a customer cannot install the wrong one by hand —
and under one key per product line it is also what selects the artifact whose
key the kit holds. `flsemi update` takes the artifact matching the chip and that
type, and refuses rather than choosing when a release offers more than one.

## Releasing

Nothing here publishes firmware. When an image is to go out, it goes through
the company's release process — the same one that governs any other delivery —
and this repository's part is only the manifest and the artifact naming above.
`releases/` is empty on purpose: a published image before P1 and the licence
check are in place would be a published image with nothing behind it.

The tool does not need the process to change: `flsemi update` verifies a digest
and uploads bytes, which is the same job whatever the process decides about
signing, encryption and who may download.

## Honest limits, to be said out loud in the licence terms

- A determined attacker with physical possession of a device they bought may
  attempt key extraction. APPROTECT and the secure partition raise the cost;
  they do not make it impossible. Under one key per product line, a single
  success is a product-line event — which is the trade that was chosen, with
  P3 as the layer that survives it.
- This is a deterrent and a clear legal boundary, not DRM.
- The nRF9151 modem firmware is Nordic's and is distributed under Nordic's
  terms; nothing here changes that.
- A public release also publishes the release cadence and version history.
  That is usually acceptable; it is a decision, not an accident.
