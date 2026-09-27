"""Run: python -m rfa_mas.poc --data-dir .local/poc --port 8780.

NemoClaw channel (P1-008M): add `--channel-bind <LAN-IP>:8010` to also serve the
authenticated core gateway for the sandbox agent (key file: <data-dir>/channel-api.key).
"""

import argparse
import ipaddress
import socket
from pathlib import Path

import uvicorn

from rfa_mas.poc.bootstrap import PocModelNotConfigured, create_poc_app
from rfa_mas.poc.channel import load_or_create_key


def parse_bind(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    try:
        address = ipaddress.ip_address(host)
        number = int(port)
    except ValueError:
        raise argparse.ArgumentTypeError("use <ip>:<port>") from None
    if address.is_loopback or address.is_unspecified or not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError(
            "channel bind must be one non-loopback host IP (the OpenShell policy host)"
        )
    return str(address), number


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
    parser.add_argument(
        "--channel-bind",
        type=parse_bind,
        default=None,
        help="serve the NemoClaw channel gateway on <lan-ip>:<port> (bearer key required)",
    )
    parser.add_argument(
        "--channel-key-file",
        type=Path,
        default=None,
        help="channel bearer key file (default <data-dir>/channel-api.key, created 0600)",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    channel = None
    channel_listener = None
    if args.channel_bind is not None:
        key_file = args.channel_key_file or (args.data_dir / "channel-api.key")
        try:
            key = load_or_create_key(key_file)
        except (OSError, ValueError):
            parser.exit(2, "poc_channel_key_invalid: check the channel key file\n")
        channel = (args.channel_bind[0], args.channel_bind[1], key)
        channel_listener = socket.socket()
        try:
            channel_listener.bind((channel[0], channel[1]))
            channel_listener.listen(128)
        except OSError:
            parser.exit(2, "poc_channel_bind_unavailable: check the LAN IP/port\n")
    # Bind before starting the app: a conflict must not initialize/change its DB.
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", args.port))
            listener.listen(128)
        except OSError:
            parser.exit(2, "poc_port_unavailable: choose another --port\n")
        try:
            app = create_poc_app(
                args.data_dir,
                port=args.port,
                model=args.model,
                env_file=args.env_file,
                channel=channel,
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
        if channel is not None:
            print(
                f"RFA NemoClaw channel: http://{channel[0]}:{channel[1]} "
                "(bearer key file, assistant routes only; OpenShell policy is the outer fence)",
                flush=True,
            )
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
        sockets = [listener] + ([channel_listener] if channel_listener is not None else [])
        server.run(sockets=sockets)
        if channel_listener is not None:
            channel_listener.close()
        if not server.started:
            parser.exit(
                2, "poc_start_failed: check data directory ownership or another running PoC\n"
            )


if __name__ == "__main__":
    main()
