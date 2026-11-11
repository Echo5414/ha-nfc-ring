#!/usr/bin/env python3
"""Pretends to be a ring, to test the integration without hardware.

Sends one hello or scan as CoAP over UDP and prints Home Assistant's answer
(2.04 accepted, 4.01 not paired or bad signature, 4.03 ring deactivated).

    ringsim.py --host ::1 --dev 0123456789abcdef --code ABCDE-12345-FGHJK-67890 hello
    ringsim.py --host ::1 --dev ... --code ... --boot 1 --n 2 scan --uid 04D00D --seq 1

--unsigned leaves the signature away (firmware before 0.4.0), --bad-mac
breaks it, --times 2 sends the identical message again as a new request (a
recorded message played back).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "nfc_ring"


def _load(name: str, directory: Path):
    spec = importlib.util.spec_from_file_location(f"nfc_ring_{name}", directory / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="::1")
    ap.add_argument("--port", type=int, default=5683)
    ap.add_argument("--pkg", type=Path, default=_PKG, help="folder with auth.py and coap.py")
    ap.add_argument("--dev", required=True, help="device id, 16 hex characters")
    ap.add_argument("--code", help="pairing code; needed unless --unsigned")
    ap.add_argument("--boot", type=int, default=1)
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--sid", type=int, default=1000)
    ap.add_argument("--unsigned", action="store_true")
    ap.add_argument("--bad-mac", action="store_true")
    ap.add_argument("--times", type=int, default=1)
    sub = ap.add_subparsers(dest="what", required=True)
    hello = sub.add_parser("hello")
    hello.add_argument("--fw", default="0.4.0")
    hello.add_argument("--trial", action="store_true", help="firmware not confirmed yet")
    hello.add_argument("--board", help="say hello like firmware 0.5.0 on, naming this board")
    hello.add_argument("--var", default="thread", help="firmware build (with --board)")
    hello.add_argument("--radio", default="thread", help="radio in use (with --board)")
    scan = sub.add_parser("scan")
    scan.add_argument("--uid", default="04D00D")
    scan.add_argument("--seq", type=int, default=1)
    args = ap.parse_args()

    auth = _load("auth", args.pkg)
    coap = _load("coap", args.pkg)

    if args.what == "hello":
        fields: dict[str, object] = {
            "dev": args.dev, "fw": args.fw, "sid": args.sid, "confirmed": not args.trial,
        }
        if args.board:
            fields.update(board=args.board, var=args.var, radio=args.radio)
            text = auth.hello2_text(
                args.dev, args.fw, args.board, args.var, args.radio,
                args.sid, not args.trial, args.boot, args.n,
            )
        else:
            text = auth.hello_text(args.dev, args.fw, args.sid, not args.trial, args.boot, args.n)
        path = coap.HELLO_PATH
    else:
        fields = {"dev": args.dev, "uid": args.uid, "sid": args.sid, "seq": args.seq}
        text = auth.scan_text(args.dev, args.uid, args.sid, args.seq, args.boot, args.n)
        path = coap.SCAN_PATH

    if not args.unsigned:
        code = auth.normalize_code(args.code or "")
        if code is None:
            ap.error("--code is not a pairing code")
        mac = auth.mac(auth.derive_key(code, args.dev), text)
        if args.bad_mac:
            mac = mac[:-1] + ("0" if mac[-1] != "0" else "1")
        fields.update(boot=args.boot, n=args.n, mac=mac)

    payload = json.dumps(fields, separators=(",", ":")).encode()
    options = [
        (coap.OPT_URI_PATH, path.encode()),
        (coap.OPT_CONTENT_FORMAT, bytes([coap.CONTENT_FORMAT_JSON])),
    ]
    family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        for _ in range(args.times):
            mid = int.from_bytes(os.urandom(2), "big")
            token = os.urandom(4)
            sock.sendto(
                coap.build_message(coap.TYPE_CON, coap.CODE_POST, mid, token, options, payload),
                (args.host, args.port),
            )
            try:
                while True:
                    data, _addr = sock.recvfrom(1500)
                    reply = coap.parse(data)
                    if reply.mid == mid:
                        break
            except TimeoutError:
                print("no answer")
                return 2
            print(f"{reply.code >> 5}.{reply.code & 0x1F:02d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
