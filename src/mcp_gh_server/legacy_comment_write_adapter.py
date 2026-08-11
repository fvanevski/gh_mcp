"""0.6.x compatibility adapter for comment creation."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context

from .legacy_write_support import raise_known_unapplied, run_write_with_metadata
from .models import CommentCreate
from .request_governor import GitHubRequestResult
from .tooling import AppContext, app_from_context, optional_created_url, require_write_enabled
from .write_contracts import execute_write_readback, legacy_write_status


async def gh_create_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    *,
    ctx: Context[AppContext],
) -> CommentCreate:
    """Post a comment on an issue or pull request."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="comment_create")
    created_url: str | None = None

    async def write() -> GitHubRequestResult[Any]:
        nonlocal created_url
        result = await run_write_with_metadata(
            app.client,
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            f"{owner}/{repo}",
            "--body-file",
            "-",
            json_output=False,
            stdin_text=body,
        )
        created_url = optional_created_url(result.value)
        return result

    async def readback() -> dict[str, Any]:
        # The 0.6.x CLI wrapper exposes only the printed comment URL and no typed
        # comment identifier. Keep the legacy schema but do not claim a structured
        # semantic readback that did not occur.
        raise RuntimeError("legacy comment creation has no structured readback contract")

    execution = await execute_write_readback(
        resource="Comment creation",
        write=write,
        readback=readback,
        state_matches_requested=lambda _value: False,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    message = status.warning or "Comment posted successfully."
    return CommentCreate(
        url=created_url or "",
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )
