"""Manage paired devices from a terminal on the core.

    python -m nora.hub pair              # a single-use code, valid five minutes
    python -m nora.hub devices           # everything paired, and its status
    python -m nora.hub approve <id>      # let a newly paired device connect
    python -m nora.hub revoke <id>       # cut a device off; takes effect live

These write the core store directly, which the running hub reads, so nothing
needs restarting.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from nora.hub import registry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nora.hub")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pair", help="create a pairing code")
    sub.add_parser("devices", help="list paired devices")
    sub.add_parser("approve").add_argument("device_id")
    sub.add_parser("revoke").add_argument("device_id")
    args = ap.parse_args(argv)

    if args.cmd == "pair":
        code, expires = registry.create_code()
        mins = round((expires - time.time()) / 60)
        print(f"Pairing code: {code}  (single use, expires in {mins} min)")
        print("Then approve the device: python -m nora.hub approve <device_id>")
        return 0
    if args.cmd == "devices":
        rows = registry.listing()
        if not rows:
            print("No devices paired.")
        for d in rows:
            seen = datetime.fromtimestamp(d.last_seen).strftime("%Y-%m-%d %H:%M") if d.last_seen else "never"
            print(f"{d.id}  {d.status:<8}  {d.platform:<8}  {d.name}  (last seen {seen})")
        return 0
    if args.cmd == "approve":
        ok = registry.approve(args.device_id)
        print("Approved." if ok else "No pending device with that id.")
        return 0 if ok else 1
    if args.cmd == "revoke":
        ok = registry.revoke(args.device_id)
        print("Revoked." if ok else "No active device with that id.")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
