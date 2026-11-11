"""CLI entry point. stdout belongs exclusively to the MCP transport."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .client import Settings
from .server import create_server


def main():
    parser = argparse.ArgumentParser(description="Toolkit HTTP adapter (MCP stdio)")
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("TOOLKIT_API_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--timeout", type=float, default=os.getenv("TOOLKIT_MCP_TIMEOUT", "120")
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=os.getenv("TOOLKIT_MCP_CONNECT_TIMEOUT", "5"),
    )
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        settings = Settings(
            api_base_url=args.api_base_url,
            timeout=args.timeout,
            connect_timeout=args.connect_timeout,
        )
    except ValueError as exc:
        parser.error(str(exc))
    create_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
