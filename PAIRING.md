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

From ring firmware 0.6.0 and integration 0.8.0 this key only pairs; see
"A key of its own" below. Older firmware signs every message with it.

## A key of its own (ring 0.6.0, integration 0.8.0)

The code is printed on the packaging and never changes. If the key from the
code signed every message, anyone who ever saw the packaging could sign as
the ring for good. So the code is only used to agree on another key:

1. A ring without a key of its own adds to its hello

       "pk": its X25519 public key (64 hex), fresh at every start
       "pm": MAC of "offer|dev|pk|boot|n"

   and signs both the hello and `pm` with the key from the code.
2. Home Assistant, once it has the ring's code, answers the hello (payload
   of the CoAP 2.04, or the Bluetooth answer) with

       {"pk": its own X25519 public key, "mac": MAC of "pair|dev|ring pk|HA pk|boot|n"}

   also under the key from the code, so the ring knows the answer comes
   from someone who has the code.
3. Both compute

       link key = HMAC-SHA256(key = X25519 shared secret,
                              msg = "nfc-ring link v1|dev|ring pk|HA pk")

   The ring stores it (`ringauth/key`) and signs everything with it from
   then on, starting with a new hello. The settings key is derived from it.
4. When Home Assistant sees the first message signed with the link key, it
   forgets the key from the code. From then on the code is worth nothing
   to anyone else: neither a hello nor a scan signed with it is accepted.

What this gives and what not:

- Someone who knows the code and listens later cannot sign as the ring and
  cannot compute the link key from a recorded pairing.
- Someone who knows the code and is active on the radio during the few
  seconds of pairing can still get in the middle. Matter has the same limit.
- Until step 4 the ring may ask again (the answer may have got lost); the
  same hello gets the same answer, a new hello a new key.
- **Deleted in Home Assistant:** the ring still signs with its link key,
  which Home Assistant no longer knows. After the first refusal the ring
  offers a new agreement in every other hello, signed with the key from
  the code; it keeps its old key until a new one is agreed. So whoever has
  the code and can answer the ring in place of Home Assistant can take the
  ring over (it then no longer works at home), but cannot sign as the ring
  towards Home Assistant.
- **Factory reset** (`ring reset yes`, or "Delete and reset ring"): the
  ring erases its link key.
- **Paired before 0.8.0:** Home Assistant still holds the key from the
  code. The first hello of an updated ring replaces it as above, without
  entering the code again.
- **Ring 0.6.0 with an older integration:** the longer hello is answered
  with 4.00; the ring then sends one without the offer and goes on with
  the key from the code, and tries again at its next regular hello. Over
  Bluetooth there is no such fallback.

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
