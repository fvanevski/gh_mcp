"""Local deployment and GitHub CLI diagnostic tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context

from .. import __version__
from ..models import ServerInfo
from ..tooling import AppContext, READ_EXTERNAL, READ_LOCAL, app_from_context, logger, mcp


@mcp.tool(
    title="Get MCP server version",
    description=(
        "Read-only local diagnostic: return this MCP server's deployed version, tool-schema "
        "version, transport, tool count, and write-policy status. This tool does not call "
        "GitHub, spawn a subprocess, request approval, or modify any state."
    ),
    annotations=READ_LOCAL,
)
async def gh_server_info(ctx: Context[AppContext]) -> ServerInfo:
    """Return deterministic local deployment metadata without contacting GitHub."""

    logger.info("MCP tool invocation reached server: tool=gh_server_info")
    app = app_from_context(ctx)
    return ServerInfo(
        server_version=__version__,
        tool_schema_version=__version__,
        transport=app.settings.transport,
        tool_count=len(await mcp.list_tools()),
        write_commands_enabled=app.settings.allow_write_commands,
        content_commits_enabled=app.settings.allow_content_commits,
        pr_merge_enabled=app.settings.allow_pr_merge,
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_info(ctx: Context[AppContext]) -> dict[str, Any]:
    """Return gh CLI version, authentication status, and active account."""

    app = app_from_context(ctx)
    auth_result = await app.client.run(
        "auth",
        "status",
        "--json",
        "hosts",
    )
    hosts = auth_result.get("hosts", {})
    version_result = await app.client.run("version", json_output=False)
    version_line = version_result.get("stdout", "") or ""
    version = (
        version_line.strip().split()[2] if len(version_line.strip().split()) > 2 else "unknown"
    )

    active_account: str | None = None
    hostname: str | None = None
    for host, accounts in hosts.items():
        if isinstance(accounts, list):
            for acct in accounts:
                if isinstance(acct, dict) and acct.get("active"):
                    active_account = acct.get("login")
                    hostname = host
                    break
        if active_account:
            break

    return {
        "version": version,
        "authenticated": active_account is not None,
        "active_account": active_account,
        "hostname": hostname,
    }
