# Pairing and message authentication (v1)

How a ring proves to Home Assistant that a message is its own. The same
scheme is meant for every radio the ring may use later (Thread today;
Bluetooth, Zigbee): it only needs a way to carry a few extra fields.

Implemented in `custom_components/nfc_ring/auth.py` (Home Assistant) and
`firmware/src/ring_auth.c` in the nfc-ring repository (ring, from 0.4.0).

## Pairing code

- 20 characters from `0123456789ABCDEFGHJKLMNPRSTUVWXY` (32 symbols, 100
  bits), printed as `XXXXX-XXXXX-XXXXX-XXXXX`. The alphabet has no I, O, Q
  or Z, so the code is also a valid Thread joiner credential.
- The ring draws it from its random generator at first start and keeps it
  in its settings (`ringauth/code`). It never sends it.
- It reaches the user on the packaging; a development board prints it with
  the shell command `ring code`.
- Typing is forgiving: case, dashes and spaces do not matter, O and I are
  read as 0 and 1.

## Key

    key = HMAC-SHA256(key = code, msg = "nfc-ring pairing v1|" + dev)

`dev` is the ring's device id (16 lower-case hex characters). Home
Assistant stores the key, not the code.

## Signed messages

`mac` is the first 8 bytes of `HMAC-SHA256(key, text)` as 16 lower-case hex
characters. `text` names the message type and every field:

| Message | Text |
|---|---|
| scan (ring → HA, `POST /nfc`) | `scan\|dev\|uid\|sid\|seq\|boot\|n` |
| hello (ring → HA, `POST /hello`) | `hello\|dev\|fw\|sid\|c\|boot\|n`, c = 1 if the firmware is confirmed, else 0 |
| update notice (HA → ring, `POST /dfu`) | `dfu\|dev\|sid\|ver\|size` |

Numbers are decimal. The JSON payloads gain three fields:

    {"dev":"…","uid":"04D00D","sid":123,"seq":7,"boot":12,"n":345,"mac":"717a3710a2623f98"}

Test vector: code `ABCDE12345FGHJK67890`, dev `0123456789abcdef`, text
`scan|0123456789abcdef|04D00D|7|3|2|5` gives mac `717a3710a2623f98`.

## Replay protection

- `boot` is stored in the ring and incremented at every start, `n` counts
  the ring's signed messages since then (hellos and scans share it). The
  pair only ever grows.
- Home Assistant keeps per ring the newest `boot`, the highest `n` and a
  64-bit window of the numbers below it. A message is accepted once.
  Anything from an older boot or below the window is refused.
- A retransmission of a message that was already accepted is acknowledged
  again but triggers nothing.
- The update notice contains `sid`, the random number the ring draws at
  every start, so a recorded notice is useless after the next restart.
  Whatever a notice makes the ring download still has to carry the
  project's firmware signature.

## States in Home Assistant

| State | Meaning | Unsigned message | Signed message |
|---|---|---|---|
| unknown | never paired, or deleted | 4.01 | 4.01; a hello is listed as "waiting for code" |
| paired | key stored | 4.01 | checked |
| not paired | known from before 0.6.0 | accepted | accepted unchecked until the code is entered |

Pairing: the user enters the code. Home Assistant derives the key for each
ring that is waiting and takes the one whose last signed message it
verifies. If no ring fits yet, the code waits for ten minutes (at most four
codes) and is tried on every hello, and on every scan of a "not paired"
ring. An unpaired ring repeats its hello every 30 seconds.

Deleting the ring's device forgets the key.

## Limits

- Signed, not encrypted. The Thread network key hides the content from
  outsiders, not from other devices on the network.
- An 8-byte MAC: a forger needs 2^63 tries on average, each one a radio
  message Home Assistant answers.
- "Not paired" rings are unauthenticated until updated and paired.
- The replay state is written to disk up to ten seconds late. After a crash
  of Home Assistant, messages from those seconds could be replayed once.
- After a restart of Home Assistant a late retransmission of an accepted
  scan is answered 4.01 instead of 2.04. Nothing is triggered twice.
- If a ring loses its settings it has a new code: delete it in Home
  Assistant and pair again.
- The keys are in `.storage/nfc_ring.rings` (readable by Home Assistant
  only) and in backups.
- Firmware downgrade to a version without signatures is not prevented by
  this scheme; the bootloader only checks the project's signature.

## Later additions

Firmware 0.5.0 adds a longer hello (`hello2|…`, with board, build and
radio), signed answers to a hello over Bluetooth (`ack|dev|boot|n`), scans
over Bluetooth (`blescan|dev|UID|boot|n`) and encrypted settings. They use
the same key and the same replay rules and are described in
[BLUETOOTH.md](BLUETOOTH.md).
