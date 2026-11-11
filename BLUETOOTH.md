# Bluetooth and radio settings (v1)

From firmware 0.5.0 a ring can use Bluetooth LE instead of Thread, and Home
Assistant can tell it which radio to use. Everything is authenticated with
the pairing key from [PAIRING.md](PAIRING.md). Code: `ble.py` (protocol),
`ble_link.py` (Home Assistant's Bluetooth), ring side `src/ble_link.c`.

Zigbee is a separate firmware build (variant `zigbee`), not a setting: it
does not fit into one image with Thread. It does not exist yet.

## When the ring advertises

Only when Bluetooth is its chosen radio, or when Thread is chosen but the
ring is not on a network. A ring that works over Thread is silent on
Bluetooth. Advertising is legacy and connectable, so it works with every
Home Assistant Bluetooth adapter and proxy that can connect.

Manufacturer data, company ID `0xFFFF` (the test ID; a product needs the
vendor's own):

    idle  A0 | dev (8) | fw major, minor, patch | flags
    scan  A1 | boot (3, LE) | n (2, LE) | tag UID (1..10) | mac (8)

Flags: 1 trial firmware, 2 Bluetooth is the chosen radio, 4 Thread attached,
8 the ring has a hello to deliver ("please connect").

A scan frame is signed (`blescan|dev|UID|boot|n`) and repeated for 2.5 s
after a scan. It does not name the ring; Home Assistant tries the keys of
its paired rings. There is no answer to an advertised scan: the ring cannot
know whether it was heard.

## Connection

Service `748a7add-0a46-47d2-8828-e902f6aa273d`, RX `748a7ade-…` (write),
TX `748a7adf-…` (notify). A message is a type byte and a payload; the first
byte of each packet is the type, with bit 7 set while more packets follow.

| Type | Direction | Content |
|---|---|---|
| 01 hello | ring → HA | hello JSON, sent on every subscribe |
| 02 ack | HA → ring | `{"st":"ok","mac":…}` signed `ack|dev|boot|n`, or `{"st":"unpaired"}` |
| 03 update notice | HA → ring | as CoAP `/dfu`, signed |
| 0A update answer | ring → HA | 0 accepted, 1 invalid, 2 unauthorized, 3 busy |
| 04 firmware request | ring → HA | offset, length (u32 LE each, at most 2048) |
| 05 firmware data | HA → ring | offset (u32 LE), bytes |
| 06 settings | HA → ring | sealed, see below |
| 07 settings answer | ring → HA | 0 ok, 1 unauthorized, 2 invalid |
| 08 scan | ring → HA | a scan frame without its first byte |

JSON to the ring must be compact (no spaces). The ring drops a connection
that brings no authenticated message within 15 s, or is idle for 20 s. Home
Assistant connects when a ring asks for it, when a setting or an update is
waiting, and backs off from rings it does not know.

## Hello from 0.5.0

The hello also carries `board`, `var` (build: `thread` or `zigbee`) and
`radio` (in use: `thread` or `bluetooth`), signed as
`hello2|dev|fw|board|var|radio|sid|c|boot|n`. Home Assistant offers a ring
only firmware for its board and build.

## Settings

    boot (u32 LE) | ctr (u32 LE) | AES-128-CCM ciphertext | tag (16)

Key: first 16 bytes of HMAC-SHA256(K, "nfc-ring cfg v1"). Nonce:
boot | sid | ctr. Additional data: `cfg|dev`. `boot` and `sid` are those of
the ring's last hello, so a recorded message is useless after the next
hello; `ctr` must grow within one. Plaintext is compact JSON with any of
`radio`, `dataset` (Thread active dataset, hex) and `server` (IPv6 address
of Home Assistant). They are encrypted because the dataset is the key to
the Thread network.

Over Thread the same bytes go to CoAP `POST /cfg` (2.04, 4.01, 4.00; 4.03
if not from the ring's server). The ring applies the settings 1.5 s after
answering and then says hello on the new radio.

A ring set to Thread that turns up over Bluetooth is given Home Assistant's
preferred Thread network and address once: that is how a new ring joins
without a phone.

## Limits

- The settings answer over Bluetooth is not signed. Home Assistant believes
  it until the ring's next (signed) hello says which radio it uses.
- The ring's Bluetooth address is fixed and the idle frame shows its device
  id: a ring on Bluetooth can be recognised by anyone nearby.
- Advertised scans are one-way and can be missed; a tag held a little
  longer is repeated.
- Checked against a real ring board from a Mac (`tools/blecheck.py`). Home
  Assistant's own Bluetooth path (`ble_link.py`) has not run against a ring
  yet.
