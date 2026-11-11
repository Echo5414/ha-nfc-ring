"""Tests for the Bluetooth protocol, settings messages and firmware file names.

Run from the repository root:  python3 -m unittest discover -s tests
The settings test needs the cryptography package and is skipped without it.
"""

import hashlib
import hmac
import importlib.util
import json
import sys
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "nfc_ring"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"nfc_ring_{name}", _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


auth = _load("auth")
coap = _load("coap")
ble = _load("ble")

DEV = "0123456789abcdef"
KEY = auth.derive_key("ABCDE12345FGHJK67890", DEV)


class FrameTest(unittest.TestCase):
    def test_idle(self):
        data = bytes([0xA0]) + bytes.fromhex(DEV) + bytes([0, 5, 0, 0x0A])
        frame = ble.parse_frame(data)
        self.assertEqual(frame, ble.IdleFrame(DEV, "0.5.0", 0x0A))
        self.assertTrue(frame.wants_connection)
        self.assertFalse(ble.IdleFrame(DEV, "0.5.0", 0x02).wants_connection)
        self.assertIsNone(ble.parse_frame(data[:-1]))
        self.assertIsNone(ble.parse_frame(b""))
        self.assertIsNone(ble.parse_frame(b"\x01" + data[1:]))

    def test_scan(self):
        mac = auth.mac(KEY, auth.ble_scan_text(DEV, "04D00D", 7, 300))
        body = (7).to_bytes(3, "little") + (300).to_bytes(2, "little") + bytes.fromhex("04D00D")
        frame = ble.parse_frame(bytes([0xA1]) + body + bytes.fromhex(mac))
        self.assertEqual(frame, ble.ScanFrame(7, 300, "04D00D", mac))
        self.assertTrue(auth.verify(KEY, auth.ble_scan_text(DEV, frame.uid, frame.boot, frame.n), frame.mac))
        # The same frame arrives without its first byte over a connection.
        self.assertEqual(ble.parse_scan(body + bytes.fromhex(mac)), frame)

    def test_scan_rejects(self):
        mac = bytes(8)
        head = (1).to_bytes(3, "little") + (1).to_bytes(2, "little")
        self.assertIsNone(ble.parse_scan(head + mac))  # no UID
        self.assertIsNone(ble.parse_scan(head + bytes(11) + mac))  # UID too long
        self.assertIsNone(ble.parse_scan(bytes(5) + b"\x04" + mac))  # boot and n start at 1
        self.assertIsNotNone(ble.parse_scan(head + bytes(10) + mac))


class PipeTest(unittest.TestCase):
    def test_split_and_reassemble(self):
        payload = bytes(range(200))
        for size in (20, 23, 199, 201, 244):
            packets = ble.split(ble.MSG_ACK, payload, size)
            self.assertTrue(all(len(p) <= size for p in packets))
            self.assertTrue(all(p[0] == ble.MSG_ACK | 0x80 for p in packets[:-1]))
            self.assertEqual(packets[-1][0], ble.MSG_ACK)
            parts = ble.Reassembler()
            got = [m for m in map(parts.feed, packets) if m is not None]
            self.assertEqual(got, [(ble.MSG_ACK, payload)])

    def test_empty_message(self):
        self.assertEqual(ble.split(ble.MSG_CFG_ACK, b"", 20), [bytes([ble.MSG_CFG_ACK])])
        self.assertEqual(ble.Reassembler().feed(bytes([ble.MSG_CFG_ACK])), (ble.MSG_CFG_ACK, b""))

    def test_split_refuses_too_long(self):
        with self.assertRaises(ValueError):
            ble.split(ble.MSG_CFG, bytes(ble.MAX_MESSAGE + 1), 244)

    def test_reassembler_drops_abandoned_and_oversized(self):
        parts = ble.Reassembler()
        self.assertIsNone(parts.feed(bytes([ble.MSG_HELLO | 0x80]) + b"abc"))
        # Another type starts: the unfinished hello is gone.
        self.assertEqual(parts.feed(bytes([ble.MSG_SCAN]) + b"xy"), (ble.MSG_SCAN, b"xy"))
        for _ in range(ble.MAX_MESSAGE // 100 + 1):
            self.assertIsNone(parts.feed(bytes([ble.MSG_HELLO | 0x80]) + bytes(100)))
        self.assertEqual(parts.feed(bytes([ble.MSG_HELLO]) + b"z"), (ble.MSG_HELLO, b"z"))

    def test_dfu_request(self):
        req = (4096).to_bytes(4, "little") + (2048).to_bytes(4, "little")
        self.assertEqual(ble.parse_dfu_request(req), (4096, 2048))
        self.assertIsNone(ble.parse_dfu_request(req[:-1]))
        self.assertIsNone(ble.parse_dfu_request(bytes(8)))  # length 0
        self.assertIsNone(ble.parse_dfu_request(bytes(4) + (2049).to_bytes(4, "little")))

    def test_dfu_packets(self):
        image = bytes(i % 251 for i in range(5000))
        packets = ble.dfu_packets(image, 2048, 2048, 244)
        self.assertTrue(all(p[0] == ble.MSG_DFU_DATA and len(p) <= 244 for p in packets))
        offsets = [int.from_bytes(p[1:5], "little") for p in packets]
        self.assertEqual(offsets[0], 2048)
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(b"".join(p[5:] for p in packets), image[2048:4096])
        # The last window is as long as what is left; nothing beyond the image.
        self.assertEqual(b"".join(p[5:] for p in ble.dfu_packets(image, 4096, 904, 20)), image[4096:])
        self.assertEqual(ble.dfu_packets(image, 4096, 905, 244), [])
        self.assertEqual(ble.dfu_packets(image, 5000, 1, 244), [])


class TextTest(unittest.TestCase):
    def test_texts(self):
        self.assertEqual(
            auth.hello2_text(DEV, "0.5.0", "nrf54l15dk", "thread", "bluetooth", 9, True, 3, 4),
            f"hello2|{DEV}|0.5.0|nrf54l15dk|thread|bluetooth|9|1|3|4",
        )
        self.assertEqual(auth.ble_scan_text(DEV, "04D00D", 3, 4), f"blescan|{DEV}|04D00D|3|4")
        self.assertEqual(auth.ack_text(DEV, 3, 4), f"ack|{DEV}|3|4")

    def test_hello_with_board(self):
        fields = {"dev": DEV, "fw": "0.5.0", "sid": 9, "confirmed": True, "boot": 3, "n": 4,
                  "mac": "00" * 8, "board": "nrf54l15dk", "var": "thread", "radio": "bluetooth"}
        hello = coap.parse_hello(json.dumps(fields).encode())
        self.assertEqual((hello.board, hello.variant, hello.radio), ("nrf54l15dk", "thread", "bluetooth"))
        for key in ("board", "var", "radio"):
            del fields[key]
        old = coap.parse_hello(json.dumps(fields).encode())
        self.assertEqual((old.board, old.variant, old.radio), (None, None, None))

    def test_seal_cfg(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESCCM
        except ImportError:
            self.skipTest("cryptography is not installed")
        settings = {"radio": "thread", "dataset": "0e08", "server": "fd00::1"}
        sealed = auth.seal_cfg(KEY, DEV, 77, 5, 1000, settings)
        self.assertEqual(sealed[:8], (5).to_bytes(4, "little") + (1000).to_bytes(4, "little"))
        cfg_key = hmac.new(KEY, b"nfc-ring cfg v1", hashlib.sha256).digest()[:16]
        nonce = (5).to_bytes(4, "little") + (77).to_bytes(4, "little") + (1000).to_bytes(4, "little")
        plain = AESCCM(cfg_key, tag_length=16).decrypt(nonce, sealed[8:], f"cfg|{DEV}".encode())
        self.assertEqual(plain, b'{"radio":"thread","dataset":"0e08","server":"fd00::1"}')
        # Bound to the ring: another device id does not open it.
        with self.assertRaises(Exception):
            AESCCM(cfg_key, tag_length=16).decrypt(nonce, sealed[8:], b"cfg|ffffffffffffffff")


class ImageNameTest(unittest.TestCase):
    def test_image_target(self):
        self.assertEqual(
            coap.image_target("nfc-ring-xiao_nrf54l15-thread-0.5.0.bin"), ("xiao_nrf54l15", "thread")
        )
        self.assertEqual(
            coap.image_target("nfc-ring-nrf54l15dk-zigbee-1.0.12.bin"), ("nrf54l15dk", "zigbee")
        )
        # Before 0.5.0 there was one board and one build.
        self.assertEqual(
            coap.image_target("nfc-ring-0.4.0.bin"), (coap.LEGACY_BOARD, coap.LEGACY_VARIANT)
        )
        for bad in ("zephyr.signed.bin", "nfc-ring-0.4.bin", "nfc-ring-dk-0.5.0.bin", "nfc-ring-a-b-0.5.0.hex"):
            self.assertIsNone(coap.image_target(bad), bad)


if __name__ == "__main__":
    unittest.main()
