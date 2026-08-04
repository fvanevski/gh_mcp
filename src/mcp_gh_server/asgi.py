"""ASGI entry point for local Streamable HTTP deployment."""

from .server import mcp

# The MCP 2026-07-28 path is stateless by construction. stateless_http=True also
# makes the compatibility path for older MCP clients stateless.
app = mcp.streamable_http_app(stateless_http=True)
