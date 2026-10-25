"""Windows desktop sidecar entry point for the Sortly local API.

The development API is normally started with:

    python -m uvicorn app.api:app --host 127.0.0.1 --port 8765

This module provides the same server through a PyInstaller-compatible entry
point. It binds to loopback only and lets Uvicorn own startup and graceful
shutdown.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import multiprocessing
import os
import signal
import sys
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SHUTDOWN_TOKEN_ENV = "SORTLY_SHUTDOWN_TOKEN"


def _configure_stdio() -> None:
    """Keep diagnostic writes safe when Windows presents a legacy text encoding."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _loopback_host(value: str) -> str:
    host = value.strip()
    if host.casefold() == "localhost":
        return host
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("desktop sidecar host must be a loopback address")


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Sortly local API sidecar.")
    parser.add_argument("--host", type=_loopback_host, default=DEFAULT_HOST)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default="info",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Load provider settings from this file without embedding it in the executable.",
    )
    return parser.parse_args(argv)


def _load_env_file(path: Path | None) -> None:
    if path is None:
        return
    env_path = path.expanduser().resolve()
    if not env_path.is_file():
        raise FileNotFoundError(f"Environment file not found: {env_path}")
    # Existing process variables take precedence and values are never logged.
    load_dotenv(dotenv_path=env_path, override=False)


def build_server(host: str, port: int, log_level: str):
    """Build a configured Uvicorn server without opening a socket."""
    import uvicorn

    from app.api import app

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level=log_level,
        loop="asyncio",
        http="h11",
        lifespan="on",
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    token = os.getenv(SHUTDOWN_TOKEN_ENV, "")
    if token:
        _install_shutdown_route(app, server, token)
    return server


def _install_shutdown_route(application, server, token: str) -> None:
    """Install a one-run, token-protected shutdown route for the desktop host."""
    from fastapi import Header, HTTPException

    @application.post("/api/shutdown", include_in_schema=False)
    def shutdown(x_sortly_shutdown_token: str | None = Header(default=None)):
        if not x_sortly_shutdown_token or not hmac.compare_digest(
            x_sortly_shutdown_token, token
        ):
            raise HTTPException(403, "Invalid shutdown token.")
        server.should_exit = True
        return {"stopping": True}


def _install_windows_break_handler(server) -> None:
    """Turn a console Ctrl+Break into Uvicorn's graceful shutdown path."""
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sys.platform != "win32" or sigbreak is None:
        return

    def request_shutdown(_signum, _frame) -> None:
        server.should_exit = True

    signal.signal(sigbreak, request_shutdown)


def _run_frozen_folder_picker(argv: Sequence[str]) -> bool:
    """Compatibility for app.api's ``sys.executable -m app.folder_picker`` call."""
    if list(argv[:2]) != ["-m", "app.folder_picker"]:
        return False
    from app.folder_picker import main as folder_picker_main

    folder_picker_main()
    return True


def main(argv: Sequence[str] | None = None) -> int:
    # This must run before any frozen child process is created on Windows.
    multiprocessing.freeze_support()
    _configure_stdio()

    arguments = list(sys.argv[1:] if argv is None else argv)
    if _run_frozen_folder_picker(arguments):
        return 0

    args = parse_args(arguments)
    try:
        _load_env_file(args.env_file)
        server = build_server(args.host, args.port, args.log_level)
        _install_windows_break_handler(server)
        server.run()
        return 0 if server.started else 1
    except Exception as error:
        print(f"Sortly sidecar could not start: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
