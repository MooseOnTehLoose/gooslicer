"""CLI: python3 -m elegoo_lan {discover|status|cmd|upload|print} ..."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import discovery, protocol, upload
from .protocol import Cmd, IncompatiblePrinterError, compatibility_warning
from .ws import SdcpWebSocket

NAMED = {"pause": Cmd.PAUSE, "stop": Cmd.STOP, "resume": Cmd.RESUME, "status": Cmd.STATUS,
         "attributes": Cmd.ATTRIBUTES, "history": Cmd.HISTORY_LIST}


def _conn(a):
    return SdcpWebSocket(a.ip, a.mainboard_id, a.id or "")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="elegoo_lan")
    sub = p.add_subparsers(dest="op", required=True)
    d = sub.add_parser("discover"); d.add_argument("--timeout", type=float, default=2.0)
    for name in ("cmd", "upload", "print", "files"):
        s = sub.add_parser(name)
        s.add_argument("ip"); s.add_argument("mainboard_id"); s.add_argument("--id", default="")
        s.add_argument("--force", action="store_true",
                       help="skip the protocol-version compatibility check")
        if name == "cmd":
            s.add_argument("command", help=f"number or one of {sorted(NAMED)}")
            s.add_argument("--data", default="{}", help="JSON args for the command")
        if name in ("upload", "print"):
            s.add_argument("file")
        if name == "files":
            s.add_argument("--url", default="/local")
    a = p.parse_args(argv)

    if a.op == "discover":
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            printers = discovery.discover(a.timeout)
        for pr in printers:
            if (msg := compatibility_warning(pr)):
                print(f"WARNING: {msg}", file=sys.stderr)
            print(json.dumps({**pr.__dict__, "supported": msg is None}, default=str))
        return 0
    if not a.force:
        try:
            pr = discovery.ensure_supported(a.ip)
        except IncompatiblePrinterError as e:
            print(f"WARNING: {e}", file=sys.stderr)
            print("Refusing to continue (use --force to override).", file=sys.stderr)
            return 2
        a.id = a.id or pr.id
    if a.op == "upload" or a.op == "print":
        upload.upload(a.ip, a.file, progress=lambda d, t: print(f"\r{d}/{t}", end="", file=sys.stderr))
        print(file=sys.stderr)
        if a.op == "upload":
            return 0
    with _conn(a) as c:
        if a.op == "print":
            rid = c.request(Cmd.START_PRINT, {"Filename": os.path.basename(a.file), "StartLayer": 0})
        elif a.op == "files":
            rid = c.request(Cmd.FILE_LIST, {"Url": a.url})
        else:
            cmd = NAMED.get(a.command) if not a.command.isdigit() else int(a.command)
            if cmd is None:
                p.error(f"unknown command {a.command}")
            rid = c.request(cmd, json.loads(a.data))
        print(json.dumps(c.wait_response(rid), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
