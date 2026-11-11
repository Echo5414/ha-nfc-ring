#!/usr/bin/env python3
"""Checks a real ring's signatures without Home Assistant.

Listens like the integration (UDP 5683), verifies every hello and scan
against the pairing code and answers 2.04 or 4.01. Point the ring here with
its shell: "ring server <this computer's IPv6 address>".

    ringcheck.py --code ABCDE-12345-FGHJK-67890 --seconds 120 --notify

--notify also sends update notices to the ring after its first valid hello:
three the ring must refuse (unsigned, wrong signature, wrong session) and a
valid one for a firmware that does not exist (the download is answered 4.04).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import select
import socket
import sys
import time
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


def code_text(code: int) -> str:
    return f"{code >> 5}.{code & 0x1F:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--code", required=True)
    ap.add_argument("--port", type=int, default=5683)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args()
    code = auth.normalize_code(args.code)
    if code is None:
        ap.error("--code is not a pairing code")

    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.bind(("::", args.port))
    window = auth.ReplayWindow()
    notices: dict[int, str] = {}  # message id -> what was sent
    notified = False
    end = time.monotonic() + args.seconds
    print(f"Listening on UDP {args.port} for {args.seconds} s", flush=True)

    while time.monotonic() < end:
        if not select.select([sock], [], [], 1)[0]:
            continue
        data, peer = sock.recvfrom(1500)
        try:
            msg = coap.parse(data)
        except ValueError as err:
            print(f"not CoAP: {err}", flush=True)
            continue
        if msg.type == coap.TYPE_ACK and msg.mid in notices:
            print(f"notice {notices.pop(msg.mid)}: ring answered {code_text(msg.code)}", flush=True)
            continue
        if msg.type != coap.TYPE_CON:
            continue
        path = msg.path
        if msg.code == coap.CODE_GET:
            print(f"GET {path}: answering 4.04", flush=True)
            sock.sendto(coap.build_ack(msg, coap.CODE_NOT_FOUND), peer)
            continue
        try:
            if path == coap.SCAN_PATH:
                m = coap.parse_event(msg.payload)
                text = None if m.mac is None else auth.scan_text(m.dev, m.uid, m.sid, m.seq, m.boot, m.n)
                what = f"scan uid={m.uid} seq={m.seq}"
            elif path == coap.HELLO_PATH:
                m = coap.parse_hello(msg.payload)
                text = None if m.mac is None else auth.hello_text(m.dev, m.fw, m.sid, m.confirmed, m.boot, m.n)
                what = f"hello fw={m.fw} confirmed={m.confirmed}"
            else:
                raise ValueError(f"unknown path {path!r}")
        except ValueError as err:
            print(f"bad message: {err}", flush=True)
            sock.sendto(coap.build_ack(msg, coap.CODE_BAD_REQUEST), peer)
            continue
        key = auth.derive_key(code, m.dev)
        if text is None:
            verdict, ok = "NOT SIGNED", False
        elif not auth.verify(key, text, m.mac):
            verdict, ok = "WRONG SIGNATURE", False
        else:
            fresh = window.check(m.boot, m.n)
            if fresh is auth.Freshness.NEW:
                window.accept(m.boot, m.n)
            verdict, ok = f"signature ok, {fresh.name.lower()}", fresh is not auth.Freshness.OLD
        print(f"{m.dev} {what} sid={m.sid} boot={m.boot} n={m.n}: {verdict}", flush=True)
        sock.sendto(coap.build_ack(msg, coap.CODE_CHANGED if ok else coap.CODE_UNAUTHORIZED), peer)

        if ok and args.notify and not notified and path == coap.HELLO_PATH:
            notified = True
            good = auth.mac(key, auth.notify_text(m.dev, m.sid, "9.9.9", 1000))
            for label, mac in (
                ("unsigned", None),
                ("wrong signature", good[:-1] + ("0" if good[-1] != "0" else "1")),
                ("wrong session", auth.mac(key, auth.notify_text(m.dev, m.sid + 1, "9.9.9", 1000))),
                ("valid", good),
            ):
                # The ring listens for notices on the CoAP port, whatever port it sent from.
                mid = int.from_bytes(os.urandom(2), "big")
                notices[mid] = label
                sock.sendto(coap.build_notify(mid, os.urandom(4), "9.9.9", 1000, mac), (peer[0], 5683))
                time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
