#!/usr/bin/env python3
"""Checks a real ring over Bluetooth without Home Assistant.

Does what the integration does: finds the ring by its advertisement,
connects, verifies its hello against the pairing code and answers it. On
request it then changes settings, waits for scans or installs a firmware.

    blecheck.py --code-file code.txt                 # find, connect, hello
    blecheck.py --code-file code.txt --radio thread  # switch the radio
    blecheck.py --code-file code.txt --scans 30      # verify scans for 30 s
    blecheck.py --code-file code.txt --dfu image.bin # install a firmware

The code file holds the pairing code and nothing else, so the code stays out
of the shell history and the process list. Needs: pip install bleak
cryptography.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

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


def compact(obj: dict) -> bytes:
    """JSON the way the ring reads it: no spaces."""
    return json.dumps(obj, separators=(",", ":")).encode()


def log(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)


class Check:
    def __init__(self, args: argparse.Namespace, code: str) -> None:
        self.args = args
        self.code = code
        self.key: bytes | None = None
        self.dev: str | None = args.dev
        self.window = auth.ReplayWindow()
        self.failed = False

    def fail(self, text: str) -> None:
        self.failed = True
        log(f"FAIL: {text}")

    def check_scan(self, scan, via: str) -> None:
        if self.key is None or self.dev is None:
            return
        text = auth.ble_scan_text(self.dev, scan.uid, scan.boot, scan.n)
        if not auth.verify(self.key, text, scan.mac):
            self.fail(f"scan {scan.uid} via {via}: wrong signature")
            return
        fresh = self.window.check(scan.boot, scan.n)
        if fresh is auth.Freshness.NEW:
            self.window.accept(scan.boot, scan.n)
            log(f"scan {scan.uid} via {via}: signature ok (boot {scan.boot}, n {scan.n})")
        elif fresh is auth.Freshness.OLD:
            self.fail(f"scan {scan.uid} via {via}: old (boot {scan.boot}, n {scan.n})")

    async def find(self):
        """Waits for the ring's idle frame; returns its Bluetooth device."""
        found: asyncio.Future = asyncio.get_running_loop().create_future()
        seen: set[bytes] = set()

        def on_adv(device, adv) -> None:
            data = adv.manufacturer_data.get(ble.COMPANY_ID)
            frame = ble.parse_frame(data) if data else None
            if isinstance(frame, ble.IdleFrame):
                if self.dev is None or frame.dev == self.dev:
                    if data not in seen:
                        seen.add(data)
                        log(
                            f"ring {frame.dev} firmware {frame.fw} flags {frame.flags:#04x} "
                            f"rssi {adv.rssi} name {adv.local_name!r}"
                        )
                    if not found.done():
                        found.set_result((device, frame))
            elif isinstance(frame, ble.ScanFrame):
                self.check_scan(frame, "advertisement")

        self.scanner = BleakScanner(on_adv)
        await self.scanner.start()
        try:
            device, frame = await asyncio.wait_for(found, self.args.timeout)
        except TimeoutError:
            await self.scanner.stop()
            self.fail("no ring advertisement seen")
            return None
        self.dev = frame.dev
        self.key = auth.derive_key(self.code, self.dev)
        return device

    async def session(self, device) -> None:
        inbox: asyncio.Queue = asyncio.Queue()
        parts = ble.Reassembler()

        def on_packet(_char, data: bytearray) -> None:
            message = parts.feed(bytes(data))
            if message is not None:
                inbox.put_nowait(message)

        async def expect(msg_type: int, seconds: float) -> bytes | None:
            deadline = time.monotonic() + seconds
            while (left := deadline - time.monotonic()) > 0:
                try:
                    got, payload = await asyncio.wait_for(inbox.get(), left)
                except TimeoutError:
                    break
                if got == msg_type:
                    return payload
                if got == ble.MSG_SCAN:
                    scan = ble.parse_scan(payload)
                    if scan is not None:
                        self.check_scan(scan, "connection")
                else:
                    log(f"(message {got:#04x}, {len(payload)} bytes)")
            return None

        async with BleakClient(device, timeout=20) as client:
            rx = client.services.get_characteristic(ble.RX_UUID)
            size = min(client.mtu_size - 3, rx.max_write_without_response_size)
            log(f"connected, MTU {client.mtu_size}, packets of {size} bytes")

            async def send(msg_type: int, payload: bytes) -> None:
                for packet in ble.split(msg_type, payload, size):
                    await client.write_gatt_char(rx, packet, response=True)

            await client.start_notify(ble.TX_UUID, on_packet)

            payload = await expect(ble.MSG_HELLO, 10)
            if payload is None:
                self.fail("no hello after subscribing")
                return
            hello = coap.parse_hello(payload)
            if hello.board is None:
                self.fail("hello without board/var/radio")
                return
            text = auth.hello2_text(
                hello.dev, hello.fw, hello.board, hello.variant, hello.radio,
                hello.sid, hello.confirmed, hello.boot, hello.n,
            )
            if hello.dev != self.dev or not auth.verify(self.key, text, hello.mac):
                self.fail("hello: wrong signature (wrong pairing code?)")
                await send(ble.MSG_ACK, b'{"st":"unpaired"}')
                return
            log(
                f"hello ok: firmware {hello.fw} board {hello.board} build {hello.variant} "
                f"radio {hello.radio} confirmed {hello.confirmed} boot {hello.boot} n {hello.n}"
            )

            if self.args.bad_ack:
                bad = compact({"st": "ok", "mac": "00" * 8})
                await send(ble.MSG_ACK, bad)
                log("sent an answer with a wrong signature; the ring must drop the link in 15 s")
                await asyncio.sleep(17)
                if client.is_connected:
                    self.fail("ring kept the connection after a forged answer")
                else:
                    log("ring dropped the connection: ok")
                return

            ack_mac = auth.mac(self.key, auth.ack_text(hello.dev, hello.boot, hello.n))
            await send(ble.MSG_ACK, compact({"st": "ok", "mac": ack_mac}))
            log("answered the hello")

            if self.args.radio or self.args.dataset_file or self.args.server:
                settings: dict = {}
                if self.args.radio:
                    settings["radio"] = self.args.radio
                if self.args.dataset_file:
                    settings["dataset"] = Path(self.args.dataset_file).read_text().strip()
                if self.args.server:
                    settings["server"] = self.args.server
                ctr = int(time.time()) & 0xFFFFFFFF

                forged = bytearray(
                    auth.seal_cfg(self.key, hello.dev, hello.sid, hello.boot, ctr, settings)
                )
                forged[-1] ^= 1
                await send(ble.MSG_CFG, bytes(forged))
                answer = await expect(ble.MSG_CFG_ACK, 5)
                if answer != bytes([ble.CFG_UNAUTHORIZED]):
                    self.fail(f"tampered settings answered {answer!r}, expected unauthorized")
                else:
                    log("tampered settings refused: ok")

                sealed = auth.seal_cfg(self.key, hello.dev, hello.sid, hello.boot, ctr + 1, settings)
                await send(ble.MSG_CFG, sealed)
                answer = await expect(ble.MSG_CFG_ACK, 5)
                if answer != bytes([ble.CFG_OK]):
                    self.fail(f"settings answered {answer!r}")
                else:
                    log(f"settings {sorted(settings)} accepted")

                await send(ble.MSG_CFG, sealed)
                answer = await expect(ble.MSG_CFG_ACK, 5)
                if answer != bytes([ble.CFG_UNAUTHORIZED]):
                    self.fail(f"replayed settings answered {answer!r}, expected unauthorized")
                else:
                    log("replayed settings refused: ok")

                payload = await expect(ble.MSG_HELLO, 8)
                if payload is not None:
                    again = coap.parse_hello(payload)
                    log(f"ring now reports radio {again.radio}")

            if self.args.dfu:
                await self.install(client, rx, size, send, expect, hello)

            if self.args.scans:
                log(f"waiting {self.args.scans} s for scans")
                await expect(-1, self.args.scans)

    async def install(self, client, rx, size, send, expect, hello) -> None:
        image = Path(self.args.dfu).read_bytes()
        version = coap.image_version(image)
        text = auth.notify_text(hello.dev, hello.sid, version, len(image))
        notice = {"size": len(image), "ver": version, "mac": auth.mac(self.key, text)}

        forged = dict(notice, mac="00" * 8)
        await send(ble.MSG_DFU, compact(forged))
        answer = await expect(ble.MSG_DFU_ACK, 5)
        if answer != bytes([ble.DFU_UNAUTHORIZED]):
            self.fail(f"forged update notice answered {answer!r}, expected unauthorized")
        else:
            log("forged update notice refused: ok")

        await send(ble.MSG_DFU, compact(notice))
        answer = await expect(ble.MSG_DFU_ACK, 5)
        if answer != bytes([ble.DFU_ACCEPTED]):
            self.fail(f"update notice answered {answer!r}")
            return
        log(f"ring accepted firmware {version} ({len(image)} bytes)")

        started = time.monotonic()
        shown = -1
        while True:
            payload = await expect(ble.MSG_DFU_REQ, 30)
            if payload is None:
                if not client.is_connected:
                    log("ring disconnected (restarting into the new firmware?)")
                else:
                    log("no further request")
                break
            request = ble.parse_dfu_request(payload)
            if request is None:
                self.fail("invalid firmware request")
                break
            offset, length = request
            packets = ble.dfu_packets(image, offset, length, size)
            if not packets:
                self.fail(f"request beyond the image: {offset}+{length}")
                break
            for packet in packets[:-1]:
                await client.write_gatt_char(rx, packet, response=False)
            await client.write_gatt_char(rx, packets[-1], response=True)
            percent = (offset + length) * 100 // len(image)
            if percent // 10 != shown:
                shown = percent // 10
                took = time.monotonic() - started
                log(f"  {percent:3d} %  {(offset + length) / max(took, 0.1) / 1024:.1f} kB/s")
            if offset + length >= len(image):
                log(f"image sent in {time.monotonic() - started:.0f} s")
                await expect(-1, 10)
                break

    async def run(self) -> None:
        device = await self.find()
        if device is None:
            return
        try:
            if not self.args.listen_only:
                await self.session(device)
            elif self.args.scans:
                log(f"waiting {self.args.scans} s for scans")
                await asyncio.sleep(self.args.scans)
        finally:
            await self.scanner.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--code-file", required=True)
    ap.add_argument("--dev", help="only this ring (16 hex digits)")
    ap.add_argument("--timeout", type=float, default=20, help="seconds to wait for the ring")
    ap.add_argument("--radio", choices=auth.CFG_RADIOS)
    ap.add_argument("--dataset-file", help="file with the Thread dataset as hex")
    ap.add_argument("--server", help="IPv6 address of Home Assistant")
    ap.add_argument("--scans", type=float, default=0, help="seconds to wait for scans")
    ap.add_argument("--listen-only", action="store_true", help="do not connect")
    ap.add_argument("--bad-ack", action="store_true", help="answer the hello with a forgery")
    ap.add_argument("--dfu", help="signed firmware image to install")
    args = ap.parse_args()
    code = auth.normalize_code(Path(args.code_file).read_text())
    if code is None:
        ap.error("the code file does not hold a pairing code")

    check = Check(args, code)
    asyncio.run(check.run())
    log("FAILED" if check.failed else "done")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
