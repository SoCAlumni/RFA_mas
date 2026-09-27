"""Run: python -m rfa_mas.inbox --port 8793 (loopback, PoC data)."""

from __future__ import annotations

import argparse
import ipaddress

import uvicorn

from rfa_mas.inbox.reference import create_inbox_reference_app


def main() -> None:
    parser = argparse.ArgumentParser(description="RFA 결재 인박스 reference API (PoC data)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8793)
    args = parser.parse_args()
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback:
        # Synthetic data only, but the admin routes mimic control actions: keep it local.
        parser.exit(2, "inbox_reference_loopback_only: bind 127.0.0.1 and proxy if needed\n")
    print(
        f"RFA inbox reference: http://{args.host}:{args.port}/docs (reference/mock)",
        flush=True,
    )
    uvicorn.run(create_inbox_reference_app(), host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
