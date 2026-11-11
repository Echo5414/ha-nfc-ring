"""Tests for pairing codes, message authentication and the replay window.

Run from the repository root:  python3 -m unittest discover -s tests
auth.py and coap.py have no Home Assistant imports, so plain Python is enough.
"""

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

DEV = "0123456789abcdef"
CODE = "ABCDE12345FGHJK67890"


class CodeTest(unittest.TestCase):
    def test_normalize_forgives_typing(self):
        self.assertEqual(auth.normalize_code("abcde-12345 fghjk-67890"), CODE)
        # O and I do not exist in the alphabet; they are read as 0 and 1.
        self.assertEqual(auth.normalize_code("OI" + CODE[2:]), "01" + CODE[2:])

    def test_normalize_rejects(self):
        for bad in ("", CODE[:-1], CODE + "A", "Z" + CODE[1:], "Q" + CODE[1:], "!" + CODE[1:]):
            self.assertIsNone(auth.normalize_code(bad), bad)

    def test_format(self):
        self.assertEqual(auth.format_code(CODE), "ABCDE-12345-FGHJK-67890")
        self.assertEqual(auth.normalize_code(auth.format_code(CODE)), CODE)


class MacTest(unittest.TestCase):
    def test_known_answer(self):
        # Fixed vector; the firmware (src/ring_auth.c) must produce the same.
        key = auth.derive_key(CODE, DEV)
        text = auth.scan_text(DEV, "04D00D", 7, 3, 2, 5)
        self.assertEqual(text, "scan|0123456789abcdef|04D00D|7|3|2|5")
        self.assertEqual(auth.mac(key, text), KNOWN_SCAN_MAC)

    def test_key_depends_on_code_and_device(self):
        key = auth.derive_key(CODE, DEV)
        self.assertNotEqual(key, auth.derive_key(CODE, "0123456789abcdee"))
        self.assertNotEqual(key, auth.derive_key("B" + CODE[1:], DEV))

    def test_every_field_is_covered(self):
        key = auth.derive_key(CODE, DEV)
        good = auth.mac(key, auth.scan_text(DEV, "04D00D", 7, 3, 2, 5))
        self.assertTrue(auth.verify(key, auth.scan_text(DEV, "04D00D", 7, 3, 2, 5), good))
        for changed in (
            auth.scan_text(DEV, "04D00E", 7, 3, 2, 5),
            auth.scan_text(DEV, "04D00D", 8, 3, 2, 5),
            auth.scan_text(DEV, "04D00D", 7, 4, 2, 5),
            auth.scan_text(DEV, "04D00D", 7, 3, 3, 5),
            auth.scan_text(DEV, "04D00D", 7, 3, 2, 6),
            auth.hello_text(DEV, "0.4.0", 7, True, 2, 5),
        ):
            self.assertFalse(auth.verify(key, changed, good), changed)

    def test_message_types_differ(self):
        self.assertTrue(auth.hello_text(DEV, "0.4.0", 7, False, 2, 5).startswith("hello|"))
        self.assertTrue(auth.hello_text(DEV, "0.4.0", 7, False, 2, 5).endswith("|0|2|5"))
        self.assertEqual(auth.notify_text(DEV, 7, "0.4.1", 320164), f"dfu|{DEV}|7|0.4.1|320164")


class ReplayTest(unittest.TestCase):
    def test_counts_up(self):
        w = auth.ReplayWindow()
        for n in range(1, 200):
            self.assertIs(w.check(1, n), auth.Freshness.NEW)
            w.accept(1, n)
            self.assertIs(w.check(1, n), auth.Freshness.SEEN)

    def test_out_of_order_inside_window(self):
        w = auth.ReplayWindow()
        w.accept(1, 10)
        self.assertIs(w.check(1, 8), auth.Freshness.NEW)
        w.accept(1, 8)
        self.assertIs(w.check(1, 8), auth.Freshness.SEEN)
        self.assertIs(w.check(1, 9), auth.Freshness.NEW)
        self.assertIs(w.check(1, 10), auth.Freshness.SEEN)

    def test_too_old(self):
        w = auth.ReplayWindow()
        w.accept(1, 100)
        self.assertIs(w.check(1, 37), auth.Freshness.NEW)  # age 63: still inside
        self.assertIs(w.check(1, 36), auth.Freshness.OLD)  # age 64: outside

    def test_new_boot_resets_and_old_boot_is_dead(self):
        w = auth.ReplayWindow()
        w.accept(3, 50)
        self.assertIs(w.check(4, 1), auth.Freshness.NEW)
        w.accept(4, 1)
        self.assertIs(w.check(3, 51), auth.Freshness.OLD)
        self.assertIs(w.check(4, 1), auth.Freshness.SEEN)
        self.assertIs(w.check(4, 2), auth.Freshness.NEW)

    def test_release_allows_retry(self):
        w = auth.ReplayWindow()
        w.accept(1, 5)
        w.accept(1, 6)
        w.release(1, 5)
        self.assertIs(w.check(1, 5), auth.Freshness.NEW)
        self.assertIs(w.check(1, 6), auth.Freshness.SEEN)

    def test_big_jump_keeps_mask_in_64_bits(self):
        w = auth.ReplayWindow()
        w.accept(1, 1)
        w.accept(1, 1000)
        self.assertEqual(w.mask, 1)
        self.assertIs(w.check(1, 1), auth.Freshness.OLD)


class PayloadTest(unittest.TestCase):
    def _scan(self, **extra):
        return json.dumps({"dev": DEV, "uid": "04D00D", "sid": 7, "seq": 3, **extra}).encode()

    def test_unsigned_scan_still_parses(self):
        event = coap.parse_event(self._scan())
        self.assertIsNone(event.mac)

    def test_signed_scan(self):
        event = coap.parse_event(self._scan(boot=2, n=5, mac="00112233445566ff"))
        self.assertEqual((event.boot, event.n, event.mac), (2, 5, "00112233445566ff"))

    def test_signature_is_all_or_nothing(self):
        for extra in (
            {"boot": 2},
            {"boot": 2, "n": 5},
            {"mac": "00112233445566ff"},
            {"boot": 0, "n": 5, "mac": "00112233445566ff"},
            {"boot": 2, "n": 0, "mac": "00112233445566ff"},
            {"boot": True, "n": 5, "mac": "00112233445566ff"},
            {"boot": 2, "n": 5, "mac": "00112233445566FF"},
            {"boot": 2, "n": 5, "mac": "0011"},
        ):
            with self.assertRaises(ValueError, msg=extra):
                coap.parse_event(self._scan(**extra))

    def test_notify_carries_mac(self):
        msg = coap.parse(coap.build_notify(1, b"\x01", "0.4.1", 1000, "00112233445566ff"))
        self.assertEqual(json.loads(msg.payload), {"ver": "0.4.1", "size": 1000, "mac": "00112233445566ff"})


KNOWN_SCAN_MAC = "717a3710a2623f98"

if __name__ == "__main__":
    unittest.main()
