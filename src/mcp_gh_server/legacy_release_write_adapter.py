"""0.6.x compatibility adapter for release creation."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context

from .legacy_write_support import raise_known_unapplied, run_write_with_metadata
from .models import ReleaseCreate
from .request_governor import GitHubRequestResult
from .tooling import AppContext, app_from_context, require_write_enabled
from .write_contracts import execute_write_readback, legacy_write_status


async def gh_create_release(
    owner: str,
    repo: str,
    tag_name: str,
    *,
    ctx: Context[AppContext],
    name: str | None = None,
    body: str | None = None,
    draft: bool = False,
    prerelease: bool = False,
    target: str | None = None,
) -> ReleaseCreate:
    """Create a new release in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host
    is responsible for user-facing approval.
    """

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="release_create")
    args = [
        "release",
        "create",
        tag_name,
        "--repo",
        f"{owner}/{repo}",
        "--notes-file",
        "-",
    ]
    if name:
        args.extend(["--title", name])
    if draft:
        args.append("--draft")
    if prerelease:
        args.append("--prerelease")
    if target:
        args.extend(["--target", target])

    async def write() -> GitHubRequestResult[Any]:
        return await run_write_with_metadata(
            app.client,
            *args,
            json_output=False,
            stdin_text=body or "",
        )

    async def readback() -> dict[str, Any]:
        fields = ["tagName", "url"]
        if name is not None:
            fields.append("name")
        if draft:
            fields.append("isDraft")
        if prerelease:
            fields.append("isPrerelease")
        value = await app.client.run(
            "release",
            "view",
            tag_name,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            ",".join(fields),
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub returned a non-object release readback")
        return value

    def matches(value: dict[str, Any]) -> bool:
        if value.get("tagName") != tag_name:
            return False
        if name is not None and value.get("name") != name:
            return False
        if draft and value.get("isDraft") is not True:
            return False
        if prerelease and value.get("isPrerelease") is not True:
            return False
        return bool(value.get("url"))

    execution = await execute_write_readback(
        resource="Release creation",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    value = execution.readback_value or {}
    message = status.warning or "Release created successfully."
    return ReleaseCreate(
        tag_name=str(value.get("tagName") or tag_name),
        url=str(value.get("url") or ""),
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )
