# NFC Ring for Home Assistant

Tap your NFC Ring on any NFC sticker and Home Assistant runs the automation
you picked for that sticker – switch a light, start a scene, anything.

The ring talks to Home Assistant over **Thread** or **Bluetooth**. This
integration receives its scans, reports them through Home Assistant's built-in **Tags** feature
and keeps the ring's firmware up to date.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Echo5414&repository=ha-nfc-ring&category=integration)

## Requirements

- Home Assistant 2026.3 or newer
- A Thread border router in Home Assistant (for example the OpenThread
  Border Router app with a Thread radio stick), or Bluetooth in Home
  Assistant (ring firmware 0.5.0 or newer)
- The Marketplace that is built into Home Assistant since 2026.11, or
  [HACS](https://hacs.xyz) on older versions

## Installation

**Home Assistant 2026.11 or newer (Marketplace):**

1. Open **Marketplace** in the sidebar, use the add-by-link button at the
   top right and enter `https://github.com/Echo5414/ha-nfc-ring`
   (type *Integration*).
2. Open *NFC Ring* in the list, click **Download**, then restart Home
   Assistant.

**Older versions (HACS):**

1. Click the button above (or in HACS: ⋮ → *Custom repositories* → add
   `https://github.com/Echo5414/ha-nfc-ring` as *Integration*).
2. Click **Download**, then restart Home Assistant.

**Then, on every version:**

**Settings → Devices & services → Add integration → NFC Ring.**

## Using it

1. Pair the ring (next section).
2. Hold the ring to an NFC sticker. The sticker appears under
   **Settings → Tags** the first time it is scanned.
3. Give it a name, then **⋮ → Create automation** and pick what should
   happen.

Every ring shows up as its own device ("NFC Ring 630E"), and automations can
tell which ring scanned a tag.

## Pairing a ring

A ring only triggers anything after it was paired with its code: 20
characters, printed on its packaging (`XXXXX-XXXXX-XXXXX-XXXXX`).

1. Switch the ring on. Once it is on your Thread network it shows up on the
   **NFC Ring** page as "New ring … – Waiting for code".
2. Enter the code under **Add a ring** and press **Pair ring**.

You can also enter the code first; the ring is then paired as soon as it
reports (a code waits for ten minutes). With the page switched off, the code
goes into **Settings → Devices & services → NFC Ring → Configure**.

- **Delete a ring:** open it from the page and choose **⋮ → Delete**. Its
  key is forgotten; to use it again, enter its code again.
- **Rings set up before version 0.6.0** keep working and are shown as "Not
  paired". Update their firmware to 0.4.0 or newer, then enter the code. A
  development board prints its code with the shell command `ring code`.
- Home Assistant stores the ring keys in `.storage/nfc_ring.rings`. They are
  part of your backups, so keep those private.

## Overview page

The integration adds an **NFC Ring** page to the sidebar. It lists every
ring (status, pairing, firmware, last scan) and every tag with the ring that scanned
it last and the automations it triggers.

- **Hide or show the page:** it is on by default. Untick it when adding the
  integration, or later under **Settings → Devices & services → NFC Ring →
  Configure**. This takes effect at once and changes nothing else.
- **Deactivate a ring** (lost, lent out): open the ring from the page and
  choose **⋮ → Disable**. Its scans are then refused until you enable it
  again.

## Choosing the radio

Each paired ring has a **Radio** choice on the NFC Ring page (and as an
entity on the ring's device): Thread or Bluetooth. Pick one; Home Assistant
tells the ring, the ring switches and reports back. If the ring cannot be
reached right now, or first needs a firmware update (0.5.0), the choice is
noted and applied as soon as possible.

- Switching to Thread sends the ring your Thread network, encrypted. A new
  ring that is not on any network yet is found over Bluetooth and put on
  your Thread network the same way.
- Bluetooth needs a Home Assistant Bluetooth adapter or proxy that can
  connect to devices. Range is a few metres around it.
- Zigbee is planned as a separate firmware and is not available yet.

Details: [BLUETOOTH.md](BLUETOOTH.md).

## Firmware updates

New ring firmware appears under **Settings → Updates** like any other
update; install it there. The ring downloads it over Thread (about two and a
half minutes) or Bluetooth and restarts.

- The ring only runs firmware signed by this project. Anything else is
  refused.
- A new firmware runs on trial. If it cannot reach Home Assistant, the ring
  goes back to the previous version on its own.

Releases come from
[Echo5414/nfc-ring-releases](https://github.com/Echo5414/nfc-ring-releases/releases).
To try your own build, put the signed `.bin` into
`/config/nfc_ring_firmware/` and restart Home Assistant; the highest
version there or on GitHub wins. The file name says what it is for:
`nfc-ring-<board>-<build>-<version>.bin`, for example
`nfc-ring-xiao_nrf54l15-thread-0.5.0.bin`. A ring is only offered images
for its own board and build. Files with other names are ignored
(`nfc-ring-<version>.bin` counts as the XIAO Thread build).

## Security

This is a prototype; do not use it for door locks or alarm systems yet.

- Every message of a paired ring is signed with a key derived from its
  pairing code, and numbered. A forged, changed or recorded-and-replayed
  scan is refused. Update notices to the ring are signed the same way.
- Messages are signed, not encrypted: someone who knows your Thread network
  key can see which tag a ring scanned.
- A ring shown as "Not paired" is not protected: anyone on your network
  could fake its scans. Pair it or delete it.
- The code is the secret. Whoever has it and is on your network can pair as
  that ring.

The protocol is described in
[PAIRING.md](PAIRING.md).

## How it works

The integration listens on UDP port 5683 (CoAP) of the Home Assistant host.

| Direction | Message | Purpose |
|---|---|---|
| ring → HA | `POST /nfc` | a tag was scanned |
| ring → HA | `POST /hello` | firmware version, after joining and hourly |
| HA → ring | `POST /dfu` | an update is available |
| ring → HA | `GET /fw/<ring>` | download the update, 512-byte blocks |

From firmware 0.4.0 on, `/nfc` and `/hello` carry `boot`, `n` and `mac`,
and `/dfu` carries `mac`; see [PAIRING.md](PAIRING.md). `tools/ringsim.py`
plays a ring without hardware, `tools/ringcheck.py` checks a real ring's
signatures, and `python3 -m unittest discover -s tests` runs the tests.
