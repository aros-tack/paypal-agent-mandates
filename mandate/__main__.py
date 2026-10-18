"""python -m mandate mcp|dashboard [--mandate examples/mandate.json] [--data ./data]"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .ledger import Ledger
from .mcp_server import McpServer
from .paypal import PayPal
from .policy import Mandate
from .service import MandateService
from .web import make_server


def build(args) -> MandateService:
    return MandateService(Mandate.load(args.mandate), Ledger(args.data), PayPal(), base_url=os.environ.get("MANDATE_BASE_URL", f"http://127.0.0.1:{args.port}"))


def main() -> None:
    ap = argparse.ArgumentParser(prog="mandate")
    ap.add_argument("command", choices=["mcp", "dashboard", "draft"])
    ap.add_argument("--mandate", default="examples/mandate.json")
    ap.add_argument("text", nargs="?", default="", help="for `draft`: the owner's request in plain language")
    ap.add_argument("--save", help="for `draft`: write the confirmed mandate to this file")
    ap.add_argument("--data", default="data")
    ap.add_argument("--port", type=int, default=8808)
    args = ap.parse_args()
    if args.command == "draft":
        from . import ai
        from .policy import MandateError
        try:
            m, notes = ai.draft_mandate(args.text)
        except MandateError as exc:
            sys.exit(f"cannot draft a mandate: {exc}")
        print(json.dumps(m.to_dict(), indent=2))
        print("\nWhat this allows:\n  - " + "\n  - ".join(notes), file=sys.stderr)
        if args.save:
            Path(args.save).write_text(json.dumps(m.to_dict(), indent=2))
            print(f"saved to {args.save} (you confirmed by passing --save)", file=sys.stderr)
        else:
            print("not saved: re-run with --save FILE to confirm", file=sys.stderr)
        return
    svc = build(args)
    if args.command == "mcp":
        McpServer(svc).serve()
    else:
        srv = make_server(svc, port=args.port)
        print(f"Mandate dashboard on http://127.0.0.1:{args.port}  (PayPal: {svc.paypal.base})", file=sys.stderr)
        srv.serve_forever()


if __name__ == "__main__":
    main()
