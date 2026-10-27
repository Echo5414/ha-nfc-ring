# NFC Ring for Home Assistant

Tap your NFC Ring on any NFC sticker and Home Assistant runs the automation
you picked for that sticker – switch a light, start a scene, anything.

The ring talks to Home Assistant over **Thread**. This integration receives
its scans, reports them through Home Assistant's built-in **Tags** feature
and keeps the ring's firmware up to date.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Echo5414&repository=ha-nfc-ring&category=integration)

## Requirements

- Home Assistant 2026.3 or newer
- A Thread border router in Home Assistant (for example the OpenThread
  Border Router app with a Thread radio stick)
- [HACS](https://hacs.xyz)

## Installation

1. Click the button above (or in HACS: ⋮ → *Custom repositories* → add
   `https://github.com/Echo5414/ha-nfc-ring` as *Integration*).
2. Click **Download**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → NFC Ring.**

## Using it

1. Hold the ring to an NFC sticker. The sticker appears under
   **Settings → Tags** the first time it is scanned.
2. Give it a name, then **⋮ → Create automation** and pick what should
   happen.

Every ring shows up as its own device ("NFC Ring 630E"), and automations can
tell which ring scanned a tag.

## Firmware updates

New ring firmware appears under **Settings → Updates** like any other
update; install it there. The ring downloads it over Thread (about two and a
half minutes) and restarts.

- The ring only runs firmware signed by this project. Anything else is
  refused.
- A new firmware runs on trial. If it cannot reach Home Assistant, the ring
  goes back to the previous version on its own.

Releases come from
[Echo5414/nfc-ring-releases](https://github.com/Echo5414/nfc-ring-releases/releases).
To try your own build, put the signed `.bin` into
`/config/nfc_ring_firmware/`; the highest version there or on GitHub wins.

## Security note

This is a prototype. Scans and ring announcements are not authenticated
yet: a device on your network could fake a tag scan. Do not use it for door
locks or alarm systems until pairing with per-ring keys is available.

## How it works

The integration listens on UDP port 5683 (CoAP) of the Home Assistant host.

| Direction | Message | Purpose |
|---|---|---|
| ring → HA | `POST /nfc` | a tag was scanned |
| ring → HA | `POST /hello` | firmware version, after joining and hourly |
| HA → ring | `POST /dfu` | an update is available |
| ring → HA | `GET /fw/<ring>` | download the update, 512-byte blocks |
