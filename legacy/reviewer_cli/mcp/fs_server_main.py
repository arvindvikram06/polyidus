"""Entry point: python -m reviewer.fs_server [ROOT ...]"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from legacy.reviewer_cli.mcp.fs_server_app import build_server
from legacy.reviewer_cli.mcp.roots import registry_from


def _configure_logging(level: str) -> None:
    """Send this server's own log to stderr; stdout may carry protocol."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    log = logging.getLogger("reviewer.fs_server")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.handlers[:] = [handler]
    log.propagate = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m reviewer.fs_server",
        description="Serve the reviewer's read-only repository tools over MCP.",
    )
    parser.add_argument(
        "roots",
        nargs="*",
        help="Repository roots this server may expose. Defaults to REVIEWER_ALLOWED_ROOTS.",
    )
    parser.add_argument(
        "--transport",
        choices=("streamable-http", "sse", "stdio"),
        default=os.environ.get("REVIEWER_MCP_TRANSPORT", "streamable-http"),
    )
    parser.add_argument("--host", default=os.environ.get("REVIEWER_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("REVIEWER_MCP_PORT", "8000")))
    parser.add_argument("--path", default=os.environ.get("REVIEWER_MCP_PATH", "/mcp"))
    parser.add_argument(
        "--log-level",
        default=os.environ.get("REVIEWER_MCP_LOG_LEVEL", "INFO"),
        help="Tool-call logging: DEBUG, INFO (default), WARNING (refusals only).",
    )
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

    try:
        registry = registry_from(args.roots)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    server = build_server(registry)

    if args.transport == "stdio":
        # stdout carries the protocol here, so nothing may be printed to it.
        server.run("stdio")
        return 0

    print(f"reviewer-fs serving {registry.describe()}", file=sys.stderr)
    print(f"  {args.transport} on http://{args.host}:{args.port}{args.path}", file=sys.stderr)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"  WARNING: bound to {args.host}, which is reachable beyond this machine. "
            "These tools expose repository contents and have no authentication.",
            file=sys.stderr,
        )
    server.run(args.transport, host=args.host, port=args.port, streamable_http_path=args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
