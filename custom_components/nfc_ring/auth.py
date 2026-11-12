"""Pairing and message authentication for the rings.

Every ring has a pairing code: 20 characters, printed on its packaging. The
ring and Home Assistant both derive the same key from it,

    key = HMAC-SHA256(code, "nfc-ring pairing v1|" + device id)

and every message carries the first 8 bytes of HMAC-SHA256(key, text) as 16
hex characters. The text names the message type and all its fields, so none
of them can be changed on the way.

Against replays each message has a pair (boot, n) that only grows: the ring
stores boot and increments it at every start, n counts the messages since
then. ReplayWindow remembers per ring which pairs were already used.

From ring firmware 0.6.0 that key only pairs. The ring's hello offers an
X25519 public key ("pk", and "pm" = MAC of offer_text), Home Assistant
answers with its own and the MAC of pair_text, both under the key from the
code, and from then on every message is signed with

    link key = HMAC-SHA256(shared secret, "nfc-ring link v1|dev|ring pk|HA pk")

Whoever reads the code off the packaging later can then neither sign as the
ring nor read what was agreed. The factory reset erases the link key.

Nothing here depends on the radio (Thread, Bluetooth, Zigbee) or on Home
Assistant; tools and tests import it on its own.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from enum import Enum, auto

# Also valid for a Thread joiner credential (no I, O, Q, Z).
ALPHABET = "0123456789ABCDEFGHJKLMNPRSTUVWXY"
CODE_LEN = 20
MAC_HEX_LEN = 16
PK_HEX_LEN = 64

_KEY_LABEL = "nfc-ring pairing v1|"
_LINK_LABEL = "nfc-ring link v1"
_WINDOW = 64
_WINDOW_MASK = (1 << _WINDOW) - 1


def normalize_code(text: str) -> str | None:
    """The code as the ring knows it, or None if text cannot be one.

    Forgives what people do when typing a code off a label: lower case,
    dashes and spaces, and the letters O and I for the digits 0 and 1.
    """
    code = "".join(text.upper().split()).replace("-", "").replace("O", "0").replace("I", "1")
    if len(code) != CODE_LEN or any(c not in ALPHABET for c in code):
        return None
    return code


def format_code(code: str) -> str:
    """The code as printed: XXXXX-XXXXX-XXXXX-XXXXX."""
    return "-".join(code[i : i + 5] for i in range(0, len(code), 5))


def derive_key(code: str, dev: str) -> bytes:
    return hmac.new(code.encode(), (_KEY_LABEL + dev).encode(), hashlib.sha256).digest()


def mac(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()[: MAC_HEX_LEN // 2].hex()


def verify(key: bytes, text: str, mac_hex: str) -> bool:
    return hmac.compare_digest(mac(key, text), mac_hex)


def scan_text(dev: str, uid: str, sid: int, seq: int, boot: int, n: int) -> str:
    return f"scan|{dev}|{uid}|{sid}|{seq}|{boot}|{n}"


def hello_text(dev: str, fw: str, sid: int, confirmed: bool, boot: int, n: int) -> str:
    return f"hello|{dev}|{fw}|{sid}|{int(confirmed)}|{boot}|{n}"


def hello2_text(
    dev: str, fw: str, board: str, variant: str, radio: str, sid: int, confirmed: bool, boot: int, n: int
) -> str:
    """Hello from firmware 0.5.0 on: also names the board, the build and the radio in use."""
    return f"hello2|{dev}|{fw}|{board}|{variant}|{radio}|{sid}|{int(confirmed)}|{boot}|{n}"


def ble_scan_text(dev: str, uid: str, boot: int, n: int) -> str:
    """Scan sent as a Bluetooth advertisement (no room for sid and seq)."""
    return f"blescan|{dev}|{uid}|{boot}|{n}"


def ack_text(dev: str, boot: int, n: int) -> str:
    """Home Assistant's answer to the hello with this boot and n, over Bluetooth."""
    return f"ack|{dev}|{boot}|{n}"


def offer_text(dev: str, pk: str, boot: int, n: int) -> str:
    """Ties the public key a hello offers to that hello."""
    return f"offer|{dev}|{pk}|{boot}|{n}"


def pair_text(dev: str, ring_pk: str, ha_pk: str, boot: int, n: int) -> str:
    """Home Assistant's answer to the offer in the hello with this boot and n."""
    return f"pair|{dev}|{ring_pk}|{ha_pk}|{boot}|{n}"


def link_key(shared: bytes, dev: str, ring_pk: str, ha_pk: str) -> bytes:
    text = f"{_LINK_LABEL}|{dev}|{ring_pk}|{ha_pk}"
    return hmac.new(shared, text.encode(), hashlib.sha256).digest()


def agree(dev: str, ring_pk: str) -> tuple[str, bytes]:
    """Our half of the key agreement: (our public key as hex, the link key).

    Raises ValueError if ring_pk is not a usable public key.
    """
    # Only pairing needs it; tools that just verify messages do not.
    from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: PLC0415
        X25519PrivateKey,
        X25519PublicKey,
    )

    private = X25519PrivateKey.generate()
    ha_pk = private.public_key().public_bytes_raw().hex()
    shared = private.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(ring_pk)))
    return ha_pk, link_key(shared, dev, ring_pk, ha_pk)


def notify_text(dev: str, sid: int, version: str, size: int) -> str:
    """Update notice from Home Assistant; sid ties it to the ring's current boot."""
    return f"dfu|{dev}|{sid}|{version}|{size}"


_CFG_LABEL = b"nfc-ring cfg v1"
CFG_RADIOS = ("thread", "bluetooth")


def seal_cfg(key: bytes, dev: str, sid: int, boot: int, ctr: int, settings: dict) -> bytes:
    """A settings message for a ring: boot, ctr, then the encrypted settings.

    settings may hold "radio", "dataset" (Thread dataset as hex) and "server"
    (IPv6 address). They are encrypted because the dataset is the key to the
    Thread network: AES-128-CCM under a key derived from the pairing key,
    bound to this ring (dev), its current start (boot, sid) and ctr, which
    must grow with every message.
    """
    # Only this function needs it; tools that just verify messages do not.
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM  # noqa: PLC0415

    cfg_key = hmac.new(key, _CFG_LABEL, hashlib.sha256).digest()[:16]
    head = boot.to_bytes(4, "little") + ctr.to_bytes(4, "little")
    nonce = boot.to_bytes(4, "little") + sid.to_bytes(4, "little") + ctr.to_bytes(4, "little")
    plain = json.dumps(settings, separators=(",", ":")).encode()
    return head + AESCCM(cfg_key, tag_length=16).encrypt(nonce, plain, f"cfg|{dev}".encode())


class Freshness(Enum):
    NEW = auto()  # never seen: accept it
    SEEN = auto()  # this exact message was accepted before (a retransmission)
    OLD = auto()  # older than what the window remembers: reject


@dataclass
class ReplayWindow:
    """Which (boot, n) pairs of one ring were already accepted.

    Remembers the highest n of the newest boot and, as a bit mask, which of
    the 64 numbers up to it were used, so messages may overtake each other a
    little. Anything from an earlier boot is old.
    """

    boot: int = 0
    top: int = 0
    mask: int = 0  # bit i: n == top - i was accepted

    def check(self, boot: int, n: int) -> Freshness:
        if boot < self.boot:
            return Freshness.OLD
        if boot > self.boot or n > self.top:
            return Freshness.NEW
        age = self.top - n
        if age >= _WINDOW:
            return Freshness.OLD
        return Freshness.SEEN if self.mask >> age & 1 else Freshness.NEW

    def accept(self, boot: int, n: int) -> None:
        """Marks a message that check() called NEW as used."""
        if boot > self.boot:
            self.boot, self.top, self.mask = boot, n, 1
        elif boot == self.boot:
            if n > self.top:
                self.mask = (self.mask << (n - self.top) | 1) & _WINDOW_MASK
                self.top = n
            elif self.top - n < _WINDOW:
                self.mask |= 1 << (self.top - n)

    def release(self, boot: int, n: int) -> None:
        """Undoes accept(), so a retransmission of a failed message may retry."""
        if boot == self.boot and 0 <= self.top - n < _WINDOW:
            self.mask &= ~(1 << (self.top - n))
