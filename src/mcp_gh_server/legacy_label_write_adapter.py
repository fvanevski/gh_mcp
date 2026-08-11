"""0.6.x compatibility adapters for label writes."""

from __future__ import annotations

import re
from typing import Any

from mcp.server.mcpserver import Context

from .legacy_write_support import raise_known_unapplied, run_write_with_metadata
from .models import LabelCreate, LabelEdit
from .request_governor import GitHubRequestResult
from .tooling import AppContext, app_from_context, get_label, require_write_enabled
from .write_contracts import execute_write_readback, legacy_write_status


async def _read_label(app: AppContext, owner: str, repo: str, name: str) -> dict[str, Any]:
    result = await get_label(app.client, owner, repo, name)
    if not isinstance(result, dict):
        raise RuntimeError("GitHub returned a non-object label readback")
    return result


async def _label_write(
    *,
    app: AppContext,
    owner: str,
    repo: str,
    name: str,
    color: str,
    description: str | None,
    force: bool,
) -> LabelCreate:
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        raise ValueError("label color must be exactly six hexadecimal characters")
    args = ["label", "create", name, "--repo", f"{owner}/{repo}", "--color", color]
    if description is not None:
        args.extend(["--description", description])
    if force:
        args.append("--force")

    async def write() -> GitHubRequestResult[Any]:
        return await run_write_with_metadata(app.client, *args, json_output=False)

    async def readback() -> dict[str, Any]:
        return await _read_label(app, owner, repo, name)

    def matches(result: dict[str, Any]) -> bool:
        if str(result.get("name") or "") != name:
            return False
        if str(result.get("color") or "").casefold() != color.casefold():
            return False
        if description is not None and result.get("description") != description:
            return False
        return True

    execution = await execute_write_readback(
        resource="Label upsert" if force else "Label creation",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    result = execution.readback_value or {}
    message = status.warning or "Label created successfully."
    return LabelCreate(
        name=str(result.get("name") or name),
        color=str(result.get("color") or color),
        description=result.get("description", description),
        url=str(result.get("url") or ""),
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )


async def gh_create_label(
    owner: str,
    repo: str,
    name: str,
    color: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
) -> LabelCreate:
    """Create a new label without overwriting an existing label."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="label_create")
    return await _label_write(
        app=app,
        owner=owner,
        repo=repo,
        name=name,
        color=color,
        description=description,
        force=False,
    )


async def gh_upsert_label(
    owner: str,
    repo: str,
    name: str,
    color: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
) -> LabelCreate:
    """Create a label or overwrite the existing label's color and description."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="label_upsert")
    return await _label_write(
        app=app,
        owner=owner,
        repo=repo,
        name=name,
        color=color,
        description=description,
        force=True,
    )


async def gh_edit_label(
    owner: str,
    repo: str,
    name: str,
    *,
    ctx: Context[AppContext],
    new_name: str | None = None,
    color: str | None = None,
    description: str | None = None,
) -> LabelEdit:
    """Edit an existing label in a repository."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="label_edit")
    if new_name is None and color is None and description is None:
        raise ValueError("at least one label edit must be provided")
    if new_name == "":
        raise ValueError("new label name cannot be empty")
    if color is not None and not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        raise ValueError("label color must be exactly six hexadecimal characters")

    args = ["label", "edit", name, "--repo", f"{owner}/{repo}"]
    if new_name is not None:
        args.extend(["--name", new_name])
    if color is not None:
        args.extend(["--color", color])
    if description is not None:
        args.extend(["--description", description])
    result_name = new_name or name

    async def write() -> GitHubRequestResult[Any]:
        return await run_write_with_metadata(app.client, *args, json_output=False)

    async def readback() -> dict[str, Any]:
        return await _read_label(app, owner, repo, result_name)

    def matches(result: dict[str, Any]) -> bool:
        if str(result.get("name") or "") != result_name:
            return False
        if color is not None and str(result.get("color") or "").casefold() != color.casefold():
            return False
        if description is not None and result.get("description") != description:
            return False
        return True

    execution = await execute_write_readback(
        resource="Label edit",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    result = execution.readback_value or {}
    message = status.warning or "Label edited successfully."
    return LabelEdit(
        name=str(result.get("name") or result_name),
        color=str(result.get("color") or color or ""),
        description=result.get("description", description),
        url=str(result.get("url") or ""),
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )
