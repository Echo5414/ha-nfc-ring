"""Ring protocol over Bluetooth LE: advertisement frames and the GATT pipe.

A ring that uses Bluetooth advertises a short frame as manufacturer data:

    idle  A0 | dev (8) | fw major, minor, patch | flags
    scan  A1 | boot (3, LE) | n (2, LE) | tag UID (1..10) | mac (8)

The idle frame is unsigned and only says "I am here"; a scan frame is signed
(auth.ble_scan_text) and repeated for a few seconds after every scan. It does
not name the ring, to fit a legacy advertisement: the receiver tries the keys
of its paired rings.

For everything longer (hello, settings, firmware) Home Assistant connects
and uses one service with two characteristics, RX (write) and TX (notify).
A message is a type byte and a payload, split into packets: the first byte
of each packet is the type, with bit 7 set while more packets follow.

Plain Python with no Home Assistant or Bluetooth imports, like coap.py, so
tools and tests use it too.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bluetooth SIG test ID. A product needs the vendor's own company ID; the
# ring firmware sets it with CONFIG_NFC_RING_BLE_COMPANY_ID.
COMPANY_ID = 0xFFFF

SERVICE_UUID = "748a7add-0a46-47d2-8828-e902f6aa273d"
RX_UUID = "748a7ade-0a46-47d2-8828-e902f6aa273d"  # Home Assistant -> ring
TX_UUID = "748a7adf-0a46-47d2-8828-e902f6aa273d"  # ring -> Home Assistant

FRAME_IDLE = 0xA0
FRAME_SCAN = 0xA1

FLAG_TRIAL = 0x01  # freshly installed firmware, not confirmed yet
FLAG_BLUETOOTH = 0x02  # Bluetooth is the ring's chosen radio
FLAG_THREAD_ATTACHED = 0x04
FLAG_WANTS_CONNECTION = 0x08  # the ring has a hello to deliver

MSG_HELLO = 0x01  # ring -> HA: hello JSON (coap.parse_hello)
MSG_ACK = 0x02  # HA -> ring: {"st":"ok","mac":...} or {"st":"unpaired"}
MSG_DFU = 0x03  # HA -> ring: update notice, same JSON as CoAP /dfu
MSG_DFU_REQ = 0x04  # ring -> HA: offset (u32 LE), length (u32 LE)
MSG_DFU_DATA = 0x05  # HA -> ring: offset (u32 LE), image bytes; one packet
MSG_CFG = 0x06  # HA -> ring: auth.seal_cfg
MSG_CFG_ACK = 0x07  # ring -> HA: CFG_* code
MSG_SCAN = 0x08  # ring -> HA: a scan frame without its first byte
MSG_DFU_ACK = 0x0A  # ring -> HA: DFU_* code

CFG_OK, CFG_UNAUTHORIZED, CFG_BAD = 0, 1, 2
DFU_ACCEPTED, DFU_BAD, DFU_UNAUTHORIZED, DFU_BUSY = 0, 1, 2, 3

_MORE = 0x80
_MAC_LEN = 8
_SCAN_FIXED = 1 + 3 + 2 + _MAC_LEN
_UID_MAX = 10

# The ring reassembles at most this much; nothing we send comes close.
MAX_MESSAGE = 700
# The ring asks for at most this much firmware per request.
MAX_DFU_WINDOW = 2048


@dataclass(frozen=True)
class IdleFrame:
    dev: str
    fw: str
    flags: int

    @property
    def wants_connection(self) -> bool:
        return bool(self.flags & FLAG_WANTS_CONNECTION)


@dataclass(frozen=True)
class ScanFrame:
    boot: int
    n: int
    uid: str  # upper-case hex
    mac: str  # lower-case hex


def parse_frame(data: bytes) -> IdleFrame | ScanFrame | None:
    """The frame in a ring's manufacturer data, or None if it is not one."""
    if not data:
        return None
    if data[0] == FRAME_IDLE and len(data) == 13:
        return IdleFrame(data[1:9].hex(), f"{data[9]}.{data[10]}.{data[11]}", data[12])
    if data[0] == FRAME_SCAN:
        return parse_scan(data[1:])
    return None


def parse_scan(body: bytes) -> ScanFrame | None:
    """A scan frame without its first byte (also the payload of MSG_SCAN)."""
    uid_len = len(body) - (_SCAN_FIXED - 1)
    if not 1 <= uid_len <= _UID_MAX:
        return None
    boot = int.from_bytes(body[0:3], "little")
    n = int.from_bytes(body[3:5], "little")
    if boot < 1 or n < 1:
        return None
    return ScanFrame(boot, n, body[5 : 5 + uid_len].hex().upper(), body[5 + uid_len :].hex())


def split(msg_type: int, payload: bytes, packet_size: int) -> list[bytes]:
    """The packets of one message; packet_size is the ATT MTU minus 3."""
    room = packet_size - 1
    if room < 1:
        raise ValueError("packet size too small")
    if len(payload) > MAX_MESSAGE:
        raise ValueError("message too long for a ring")
    chunks = [payload[i : i + room] for i in range(0, len(payload), room)] or [b""]
    return [
        bytes([msg_type | (_MORE if i < len(chunks) - 1 else 0)]) + chunk
        for i, chunk in enumerate(chunks)
    ]


class Reassembler:
    """Puts the packets from a ring back together into messages."""

    def __init__(self) -> None:
        self._type: int | None = None
        self._data = bytearray()

    def feed(self, packet: bytes) -> tuple[int, bytes] | None:
        """One packet in; a complete (type, payload) out, or None."""
        if not packet:
            return None
        msg_type = packet[0] & ~_MORE & 0xFF
        if self._type is not None and self._type != msg_type:
            self._data.clear()  # the ring gave up on the previous message
        self._type = msg_type
        self._data += packet[1:]
        if len(self._data) > MAX_MESSAGE:
            self._type = None
            self._data.clear()
            return None
        if packet[0] & _MORE:
            return None
        message = (msg_type, bytes(self._data))
        self._type = None
        self._data.clear()
        return message


def parse_dfu_request(payload: bytes) -> tuple[int, int] | None:
    """(offset, length) the ring asks for, or None if the request is invalid."""
    if len(payload) != 8:
        return None
    offset = int.from_bytes(payload[0:4], "little")
    length = int.from_bytes(payload[4:8], "little")
    if not 0 < length <= MAX_DFU_WINDOW:
        return None
    return offset, length


def dfu_packets(image: bytes, offset: int, length: int, packet_size: int) -> list[bytes]:
    """The MSG_DFU_DATA packets answering one request, in the order to send.

    Each is a message of its own (the ring wants them unsplit and in order).
    Empty if the request reaches beyond the image.
    """
    room = packet_size - 1 - 4
    if room < 1:
        raise ValueError("packet size too small")
    if offset >= len(image) or offset + length > len(image):
        return []
    return [
        bytes([MSG_DFU_DATA]) + pos.to_bytes(4, "little") + image[pos : min(pos + room, offset + length)]
        for pos in range(offset, offset + length, room)
    ]
