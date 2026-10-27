"""Ring protocol: CoAP parsing/encoding, message validation and duplicate filtering.

Plain Python with no Home Assistant imports, so tools/coap_test_server.py
can use it too.
"""

from __future__ import annotations

import json
import re
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto

TYPE_CON, TYPE_NON, TYPE_ACK, TYPE_RST = 0, 1, 2, 3
CODE_EMPTY = 0x00
CODE_GET = 0x01
CODE_POST = 0x02
CODE_CHANGED = 0x44  # 2.04
CODE_CONTENT = 0x45  # 2.05
CODE_BAD_REQUEST = 0x80  # 4.00
CODE_BAD_OPTION = 0x82  # 4.02
CODE_FORBIDDEN = 0x83  # 4.03
CODE_NOT_FOUND = 0x84  # 4.04
CODE_METHOD_NOT_ALLOWED = 0x85  # 4.05
CODE_INTERNAL_ERROR = 0xA0  # 5.00
CODE_SERVICE_UNAVAILABLE = 0xA3  # 5.03

OPT_URI_PATH = 11
OPT_CONTENT_FORMAT = 12
OPT_BLOCK2 = 23
CONTENT_FORMAT_JSON = 50

# Critical options (odd numbers) we understand; any other one must be
# rejected rather than silently ignored (RFC 7252 5.4.1).
_SUPPORTED_CRITICAL_OPTIONS = {OPT_URI_PATH, OPT_BLOCK2}

SCAN_PATH = "nfc"
HELLO_PATH = "hello"
FIRMWARE_PATH = "fw"  # GET fw/<device id> with Block2
NOTIFY_PATH = "dfu"  # POST to the ring: an update is waiting

# A scan or hello is ~80 bytes; the cap also keeps json.loads far from
# Python's recursion limit on hostile input like "[[[[...".
MAX_PAYLOAD = 256

# Largest Block2 size we serve (SZX 6 = 1024 bytes).
MAX_BLOCK_SZX = 6

# Longer than CoAP's EXCHANGE_LIFETIME (~247 s), so every retransmission of
# a scan is still recognised.
DEDUP_SECONDS = 300

# Bounds memory and concurrent work if something floods the port.
MAX_TRACKED_SCANS = 4096
MAX_PENDING_SCANS = 16

MCUBOOT_IMAGE_MAGIC = 0x96F3B83D

_DEV_RE = re.compile(r"[0-9a-f]{16}")
_UID_RE = re.compile(r"(?:[0-9A-F]{2}){1,10}")
_VERSION_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,5}")
_UINT32_MAX = 0xFFFFFFFF


@dataclass(frozen=True)
class CoapMessage:
    type: int
    code: int
    mid: int
    token: bytes
    path: str
    payload: bytes
    unsupported_critical_option: bool
    block2: int | None = None  # raw Block2 option value


@dataclass(frozen=True)
class ScanEvent:
    dev: str  # ring's IEEE EUI-64, lower-case hex
    uid: str  # tag UID, upper-case hex
    sid: int  # random per ring boot
    seq: int  # scan counter within a boot

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.dev, self.sid, self.seq)


@dataclass(frozen=True)
class Hello:
    dev: str
    fw: str  # running firmware version, e.g. "0.2.0"
    sid: int
    confirmed: bool  # False while a freshly installed firmware is on trial


@dataclass(frozen=True)
class Block:
    num: int
    more: bool
    szx: int

    @property
    def size(self) -> int:
        return 16 << self.szx


def _option_field(data: bytes, pos: int, nibble: int) -> tuple[int, int]:
    """Decodes an option delta/length nibble plus its extended bytes."""
    if nibble < 13:
        return nibble, pos
    if nibble == 15:
        raise ValueError("reserved option nibble")
    size, base = (1, 13) if nibble == 13 else (2, 269)
    if pos + size > len(data):
        raise ValueError("truncated option header")
    return int.from_bytes(data[pos : pos + size], "big") + base, pos + size


def parse(data: bytes) -> CoapMessage:
    """Parses a CoAP datagram; raises ValueError if it is malformed."""
    if len(data) < 4:
        raise ValueError("short header")
    ver, typ, tkl = data[0] >> 6, (data[0] >> 4) & 3, data[0] & 0xF
    if ver != 1 or tkl > 8 or len(data) < 4 + tkl:
        raise ValueError("bad header")
    code, mid = data[1], int.from_bytes(data[2:4], "big")
    token = data[4 : 4 + tkl]
    pos, opt, path, payload = 4 + tkl, 0, [], b""
    unsupported_critical = False
    block2 = None
    while pos < len(data):
        if data[pos] == 0xFF:
            payload = data[pos + 1 :]
            if not payload:
                raise ValueError("payload marker without payload")
            break
        header = data[pos]
        delta, pos = _option_field(data, pos + 1, header >> 4)
        length, pos = _option_field(data, pos, header & 0xF)
        if pos + length > len(data):
            raise ValueError("truncated option value")
        opt += delta
        value = data[pos : pos + length]
        if opt == OPT_URI_PATH:
            path.append(value.decode())
        elif opt == OPT_BLOCK2:
            if length > 3:
                raise ValueError("Block2 option too long")
            block2 = int.from_bytes(value, "big")
        elif opt % 2 and opt not in _SUPPORTED_CRITICAL_OPTIONS:
            unsupported_critical = True
        pos += length
    return CoapMessage(typ, code, mid, token, "/".join(path), payload, unsupported_critical, block2)


def decode_block(value: int) -> Block:
    """Block2 value: NUM << 4 | M << 3 | SZX (RFC 7959)."""
    szx = value & 0x7
    if szx == 7:
        raise ValueError("reserved block size")
    return Block(value >> 4, bool(value & 0x8), szx)


def _encode_uint(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _encode_options(options: list[tuple[int, bytes]]) -> bytes:
    out = bytearray()
    last = 0
    for number, value in sorted(options, key=lambda option: option[0]):
        delta, length = number - last, len(value)
        last = number
        header_ext = bytearray()

        def nibble(n: int) -> int:
            if n < 13:
                return n
            if n < 269:
                header_ext.append(n - 13)
                return 13
            header_ext.extend((n - 269).to_bytes(2, "big"))
            return 14

        delta_nibble = nibble(delta)
        length_nibble = nibble(length)
        out.append(delta_nibble << 4 | length_nibble)
        out += header_ext
        out += value
    return bytes(out)


def build_message(
    typ: int,
    code: int,
    mid: int,
    token: bytes,
    options: list[tuple[int, bytes]] | None = None,
    payload: bytes = b"",
) -> bytes:
    header = bytes([0x40 | (typ << 4) | len(token), code]) + mid.to_bytes(2, "big") + token
    body = _encode_options(options or [])
    if payload:
        body += b"\xff" + payload
    return header + body


def build_ack(msg: CoapMessage, code: int) -> bytes:
    """Piggybacked response without payload to a confirmable request."""
    return build_message(TYPE_ACK, code, msg.mid, msg.token)


def build_block_response(msg: CoapMessage, block: Block, data: bytes) -> bytes:
    """Piggybacked 2.05 carrying one Block2 block of data."""
    value = block.num << 4 | int(block.more) << 3 | block.szx
    return build_message(
        TYPE_ACK, CODE_CONTENT, msg.mid, msg.token, [(OPT_BLOCK2, _encode_uint(value))], data
    )


def build_notify(mid: int, token: bytes, version: str, size: int) -> bytes:
    """Confirmable POST /dfu telling a ring to download an update."""
    payload = json.dumps({"ver": version, "size": size}, separators=(",", ":")).encode()
    options = [
        (OPT_URI_PATH, NOTIFY_PATH.encode()),
        (OPT_CONTENT_FORMAT, _encode_uint(CONTENT_FORMAT_JSON)),
    ]
    return build_message(TYPE_CON, CODE_POST, mid, token, options, payload)


def _load_object(payload: bytes) -> dict:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload of {len(payload)} bytes is too large")
    obj = json.loads(payload)
    if not isinstance(obj, dict):
        raise ValueError("payload is not an object")
    return obj


def _uint32(value: object) -> bool:
    # bool is a subclass of int; JSON true/false must not pass as numbers.
    return type(value) is int and 0 <= value <= _UINT32_MAX


def _check_dev(dev: object) -> str:
    if not (isinstance(dev, str) and _DEV_RE.fullmatch(dev)):
        raise ValueError(f"bad dev {dev!r}")
    return dev


def parse_event(payload: bytes) -> ScanEvent:
    """Validates a scan payload; raises ValueError if it is not a valid scan."""
    event = _load_object(payload)
    dev, uid, sid, seq = (event.get(k) for k in ("dev", "uid", "sid", "seq"))
    _check_dev(dev)
    if not (isinstance(uid, str) and _UID_RE.fullmatch(uid)):
        raise ValueError(f"bad uid {uid!r}")
    if not (_uint32(sid) and _uint32(seq)):
        raise ValueError("bad sid/seq")
    return ScanEvent(dev, uid, sid, seq)


def parse_hello(payload: bytes) -> Hello:
    """Validates a hello payload; raises ValueError if it is invalid."""
    hello = _load_object(payload)
    dev, fw, sid = (hello.get(k) for k in ("dev", "fw", "sid"))
    # Firmware before 0.3.2 does not send it and never runs on trial.
    confirmed = hello.get("confirmed", True)
    _check_dev(dev)
    if not (isinstance(fw, str) and _VERSION_RE.fullmatch(fw)):
        raise ValueError(f"bad fw {fw!r}")
    if not _uint32(sid):
        raise ValueError("bad sid")
    if not isinstance(confirmed, bool):
        raise ValueError("bad confirmed")
    return Hello(dev, fw, sid, confirmed)


def image_version(image: bytes) -> str:
    """Version from an MCUboot image header; ValueError if it is not one.

    Header: magic, load address (u32), header size, protected TLV size
    (u16 each), image size, flags (u32), then version major, minor (u8),
    revision (u16), build (u32), all little-endian.
    """
    if len(image) < 32:
        raise ValueError("too short for an MCUboot header")
    magic, _, _, _, _, _, major, minor, revision, _ = struct.unpack_from("<IIHHIIBBHI", image)
    if magic != MCUBOOT_IMAGE_MAGIC:
        raise ValueError("not an MCUboot image")
    return f"{major}.{minor}.{revision}"


def is_version(text: str) -> bool:
    """True for a firmware version like "0.3.2"."""
    return _VERSION_RE.fullmatch(text) is not None


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class ScanState(Enum):
    NEW = auto()  # first copy: caller must call finish() or forget()
    PENDING = auto()  # retransmission while the first copy is still being recorded
    DONE = auto()  # retransmission of a recorded scan
    FULL = auto()  # too many scans tracked or in progress; not admitted


@dataclass
class DuplicateFilter:
    """Tracks scan keys for DEDUP_SECONDS to recognise CoAP retransmissions."""

    # key -> [first seen, recorded], oldest first
    _seen: OrderedDict[tuple[str, int, int], list] = field(default_factory=OrderedDict)
    _pending: int = 0

    def admit(self, key: tuple[str, int, int]) -> ScanState:
        now = time.monotonic()
        while self._seen and next(iter(self._seen.values()))[0] < now - DEDUP_SECONDS:
            _, (_, recorded) = self._seen.popitem(last=False)
            if not recorded:
                self._pending -= 1

        if (entry := self._seen.get(key)) is not None:
            return ScanState.DONE if entry[1] else ScanState.PENDING
        if len(self._seen) >= MAX_TRACKED_SCANS or self._pending >= MAX_PENDING_SCANS:
            return ScanState.FULL

        self._seen[key] = [now, False]
        self._pending += 1
        return ScanState.NEW

    def finish(self, key: tuple[str, int, int]) -> None:
        """Marks an admitted scan as recorded."""
        entry = self._seen.get(key)
        if entry is not None and not entry[1]:
            entry[1] = True
            self._pending -= 1

    def forget(self, key: tuple[str, int, int]) -> None:
        """Lets a retransmission through again, e.g. after a failed scan."""
        entry = self._seen.pop(key, None)
        if entry is not None and not entry[1]:
            self._pending -= 1
