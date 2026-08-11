"""0.6.x compatibility adapter for milestone creation."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from mcp.server.mcpserver import Context

from .legacy_write_support import raise_known_unapplied, run_write_with_metadata
from .models import MilestoneCreate
from .request_governor import GitHubRequestResult
from .tooling import AppContext, app_from_context, created_json, require_write_enabled
from .write_contracts import execute_write_readback, legacy_write_status


async def gh_create_milestone(
    owner: str,
    repo: str,
    title: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
    due_on: str | None = None,
    state: str = "open",
) -> MilestoneCreate:
    """Create a new milestone in a repository via the GitHub API."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="milestone_create")
    if state not in {"open", "closed"}:
        raise ValueError("milestone state must be open or closed")

    payload: dict[str, Any] = {"title": title, "state": state}
    if description is not None:
        payload["description"] = description
    if due_on:
        payload["due_on"] = due_on

    number: int | None = None
    created: dict[str, Any] = {}

    async def write() -> GitHubRequestResult[Any]:
        nonlocal number, created
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as file:
            json.dump(payload, file)
            file.flush()
            payload_path = file.name
        try:
            result = await run_write_with_metadata(
                app.client,
                "api",
                f"repos/{owner}/{repo}/milestones",
                "-X",
                "POST",
                "--input",
                payload_path,
                json_output=False,
            )
        finally:
            os.unlink(payload_path)
        try:
            created = created_json(result.value, "Milestone")
        except RuntimeError:
            created = {}
        raw_number = created.get("number")
        number = raw_number if isinstance(raw_number, int) else None
        return result

    async def readback() -> dict[str, Any]:
        if number is None:
            raise RuntimeError("milestone creation returned no number for authoritative readback")
        result = await app.client.run("api", f"repos/{owner}/{repo}/milestones/{number}")
        if not isinstance(result, dict):
            raise RuntimeError("GitHub returned a non-object milestone readback")
        return result

    def matches(result: dict[str, Any]) -> bool:
        if result.get("number") != number or result.get("title") != title:
            return False
        if str(result.get("state") or "") != state:
            return False
        if description is not None and result.get("description") != description:
            return False
        if due_on is not None:
            actual_due = result.get("due_on")
            if not isinstance(actual_due, str) or not actual_due.startswith(due_on):
                return False
        return True

    execution = await execute_write_readback(
        resource="Milestone creation",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    result = execution.readback_value or created
    result_number = result.get("number")
    final_number = result_number if isinstance(result_number, int) else (number or 0)
    message = status.warning or "Milestone created successfully."
    return MilestoneCreate(
        number=final_number,
        title=str(result.get("title") or title),
        url=str(result.get("url") or ""),
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )
