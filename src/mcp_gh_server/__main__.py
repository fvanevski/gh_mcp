"""Console entry point."""

from __future__ import annotations

import logging
import sys

from .server import mcp
from .settings import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if settings.transport == "stdio":
        mcp.run()
        return

    mcp.run(
        "streamable-http",
        host=settings.http_host,
        port=settings.http_port,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
