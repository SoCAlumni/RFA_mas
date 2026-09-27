"""Run: python -m rfa_mas.poc --data-dir .local/poc --port 8780."""

import argparse
import socket
from pathlib import Path

import uvicorn

from rfa_mas.poc.bootstrap import PocModelNotConfigured, create_poc_app


def main():
    parser = argparse.ArgumentParser(
        description="RFA local PoC (mock model/publication; no sandbox)"
    )
    parser.add_argument("--data-dir", type=Path, default=Path(".local/poc"))
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument(
        "--model",
        choices=["mock", "nvidia"],
        default="mock",
        help="nvidia: real NVIDIA chat model; implies owner consent (ALLOW_EXTERNAL_EGRESS) "
        "to send the owner's own KB context to that endpoint for owner-target answers",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="env file with NVIDIA_BASE_URL/NVIDIA_MODEL/NVIDIA_API_KEY (values never printed)",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    # Bind before starting the app: a conflict must not initialize/change its DB.
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", args.port))
            listener.listen(128)
        except OSError:
            parser.exit(2, "poc_port_unavailable: choose another --port\n")
        try:
            app = create_poc_app(
                args.data_dir, port=args.port, model=args.model, env_file=args.env_file
            )
        except PocModelNotConfigured as exc:
            parser.exit(2, f"{exc}\n")
        except (OSError, ValueError):
            parser.exit(2, "poc_data_dir_invalid: use a dedicated writable directory\n")
        mode = (
            "local/mock"
            if args.model == "mock"
            else "real NVIDIA model, owner-consented egress; publication/runtime still local"
        )
        print(f"RFA PoC: http://127.0.0.1:{args.port}/ui/ ({mode}; Ctrl-C to stop)", flush=True)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=args.port,
                access_log=False,
                log_level="warning",
                proxy_headers=False,
            )
        )
        server.run(sockets=[listener])
        if not server.started:
            parser.exit(
                2, "poc_start_failed: check data directory ownership or another running PoC\n"
            )


if __name__ == "__main__":
    main()
