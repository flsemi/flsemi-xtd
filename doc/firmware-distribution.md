# Firmware distribution from GitHub, bound to hardware we sold

Decided 2026-09-22: **per-batch encryption keys**, and all three layers.
This is the contract between the tool, the two firmware projects and the
manufacturing process. It is a design note, not a shipped feature.

## The two properties, kept apart

| | what it stops | mechanism | state today |
|---|---|---|---|
| **P1** | someone else's firmware running on our hardware | MCUboot image signature with **our own key**, plus APPROTECT | **absent** — both bootloaders use the MCUboot/NCS default development key (no `CONFIG_BOOT_SIGNATURE_KEY_FILE` in either project) |
| **P2** | our firmware running on hardware we did not sell | MCUboot **encrypted images**, key held only by devices we provisioned | absent — `CONFIG_BOOT_ENCRYPT_IMAGE=n` |
| **P3** | a decrypted image lifted off a legitimate device and run elsewhere | run-time licence blob signed over the FICR DEVICEID, checked in the secure image | absent; the device-id registers it would bind to already exist |

A signature gives P1 only. It is routinely mistaken for P2, and it is not.

## Why "an encoded hex on GitHub" is not a mechanism

If the customer's updater can turn the published file into something the
chip will run, the key is in the updater, and the updater is in the
customer's hands. Obfuscation changes who bothers, not who can.

The only arrangement that survives publication is: **the ciphertext is
public, the key is in the device, and nothing on the host ever holds the
plaintext.** MCUboot decrypts in the bootloader, on-chip, during the swap.
`xtd` moves bytes it cannot read.

That is what makes a public GitHub release acceptable: to anyone without
one of our boards, the artifact is inert.

## Why per batch

The mesh distributes one image between nodes (`nrota`, EP 0x2A3C). Per-device
keys would mean a distinct ciphertext per node, which breaks mesh OTA outright
and does not fit in a GitHub release. Per-product-line keys keep everything
working but make one extracted key the end of the product line's protection.

Per batch is the middle: nodes of one batch share a ciphertext, so mesh OTA is
unchanged within a batch; an extracted key costs one batch and the next batch
ships a new one.

**Consequence for manufacturing:** MCUboot holds the image-encryption private
key, so the *bootloader* is batch-specific and is flashed at manufacture. The
batch key never leaves the signing machine and the device.

**Consequence for the mesh:** a node must not pull an image it cannot decrypt.
The OTA stamp (`nrstamp.h`: `build_time`, `build_id`, `image_type`, `flags`,
`check`) needs a **batch id**, and the QUERY/INFO exchange must compare it —
otherwise a mixed-batch site produces download-then-fail loops that look like
a radio problem.

## What each side owns

**BFi53USB7IP (nRF5340)**
1. Own signing key (`CONFIG_BOOT_SIGNATURE_KEY_FILE`), replacing the default.
2. `CONFIG_BOOT_ENCRYPT_IMAGE=y` with the batch KEK.
3. Expose the **batch id** on the configuration plane — the natural home is
   the `chipid` read (id 25), beside the two device ids.
4. APPROTECT on production units.

**BFi91NR7DLCVG (nRF9151)**
1. Own signing key for the MCUboot used by the `dfu91` serial-recovery path.
2. `CONFIG_BOOT_ENCRYPT_IMAGE=y`, same batch KEK scheme.
3. **Batch id in the image stamp**, and in the OTA QUERY/INFO comparison, so a
   node only pulls what it can decrypt.
4. P3: licence blob bound to FICR DEVICEID (0x1FF0), checked in the secure
   image rather than the non-secure application.

**Manufacturing / business**
- Who holds the private keys, and on what machine. An offline machine is the
  minimum; an HSM is the honest answer for a key that gates a product line.
- Batch key generation, the record of which serial numbers belong to which
  batch, and what a key rotation looks like.

**flsemi-xtd (this repository)**
- `xtd update`: read the device, fetch the manifest, pick the artifact that
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
      "batch": "2026Q4-A",
      "version": "0.6.3",
      "file": "BFi53USB7IP-0.6.3-2026Q4-A.enc.bin",
      "sha256": "…",
      "size": 816192,
      "encrypted": true
    },
    {
      "chip": "nrf9151",
      "image_type": "0x706b1e6a",
      "batch": "2026Q4-A",
      "version": "1.520",
      "file": "BFi91NR7DLCVG-1.520-2026Q4-A.enc.bin",
      "sha256": "…",
      "size": 450188,
      "encrypted": true
    }
  ]
}
```

`image_type` is the stamp field that already distinguishes a Thingy:91 X image
from an nRF9151 DK image, so a customer cannot install the wrong one by hand.
`batch` is the new field, and until the firmware reports it, `xtd update`
refuses to guess: it says the device does not report a batch and asks for
`--batch`.

## Honest limits, to be said out loud in the licence terms

- A determined attacker with physical possession of a device they bought may
  attempt key extraction. APPROTECT and the secure partition raise the cost;
  they do not make it impossible.
- This is a deterrent and a clear legal boundary, not DRM.
- The nRF9151 modem firmware is Nordic's and is distributed under Nordic's
  terms; nothing here changes that.
- A public release also publishes the release cadence and version history.
  That is usually acceptable; it is a decision, not an accident.
