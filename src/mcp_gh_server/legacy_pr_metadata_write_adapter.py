"""0.6.x compatibility adapters for pull-request create/edit writes."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context

from .legacy_write_support import raise_known_unapplied, run_write_with_metadata
from .models import PullRequestCreate, PullRequestEdit
from .request_governor import GitHubRequestResult
from .tooling import (
    AppContext,
    app_from_context,
    optional_created_url,
    require_write_enabled,
    trailing_number,
)
from .write_contracts import execute_write_readback, legacy_write_status


def _names(items: Any, key: str) -> set[str]:
    if not isinstance(items, list):
        return set()
    return {
        str(item.get(key))
        for item in items
        if isinstance(item, dict) and isinstance(item.get(key), str)
    }


async def gh_create_pr(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
    *,
    ctx: Context[AppContext],
    draft: bool = False,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    review_users: list[str] | None = None,
) -> PullRequestCreate:
    """Create a new pull request in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host
    is responsible for user-facing approval.
    """

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_create")
    args = [
        "pr",
        "create",
        "--repo",
        f"{owner}/{repo}",
        "--title",
        title,
        "--body-file",
        "-",
        "--head",
        head,
        "--base",
        base,
    ]
    if draft:
        args.append("--draft")
    if labels:
        for label in labels:
            args.extend(["--label", label])
    if assignees:
        for assignee in assignees:
            args.extend(["--assignee", assignee])
    if review_users:
        for user in review_users:
            args.extend(["--reviewer", user])

    created_url: str | None = None

    async def write() -> GitHubRequestResult[Any]:
        nonlocal created_url
        result = await run_write_with_metadata(
            app.client,
            *args,
            json_output=False,
            stdin_text=body,
        )
        created_url = optional_created_url(result.value)
        return result

    async def readback() -> dict[str, Any]:
        if created_url is None:
            raise RuntimeError("pull request creation returned no stable URL for readback")
        fields = (
            "title,number,url,body,headRefName,baseRefName,isDraft,"
            "labels,assignees,reviewRequests"
        )
        value = await app.client.run(
            "pr",
            "view",
            created_url,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            fields,
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub returned a non-object pull request readback")
        return value

    def matches(value: dict[str, Any]) -> bool:
        if value.get("title") != title or str(value.get("body") or "") != body:
            return False
        if value.get("headRefName") != head and value.get("headRefName") != head.split(":", 1)[-1]:
            return False
        if value.get("baseRefName") != base:
            return False
        if bool(value.get("isDraft", False)) is not draft:
            return False
        if labels and not set(labels).issubset(_names(value.get("labels"), "name")):
            return False
        if assignees and not set(assignees).issubset(_names(value.get("assignees"), "login")):
            return False
        if review_users and not set(review_users).issubset(
            _names(value.get("reviewRequests"), "login")
        ):
            return False
        return isinstance(value.get("number"), int) and bool(value.get("url"))

    execution = await execute_write_readback(
        resource="Pull request creation",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    value = execution.readback_value or {}
    raw_number = value.get("number")
    pr_number = raw_number if isinstance(raw_number, int) else trailing_number(created_url)
    message = status.warning or "Pull request created successfully."
    return PullRequestCreate(
        number=pr_number,
        title=str(value.get("title") or title),
        url=str(value.get("url") or created_url or ""),
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )


async def gh_edit_pr(
    owner: str,
    repo: str,
    number: int,
    *,
    ctx: Context[AppContext],
    title: str | None = None,
    body: str | None = None,
    labels_add: list[str] | None = None,
    labels_remove: list[str] | None = None,
    assignees_add: list[str] | None = None,
    assignees_remove: list[str] | None = None,
    base: str | None = None,
) -> PullRequestEdit:
    """Edit an existing pull request."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_edit")
    if not any(
        (
            title is not None,
            body is not None,
            labels_add,
            labels_remove,
            assignees_add,
            assignees_remove,
            base is not None,
        )
    ):
        raise ValueError("at least one pull request edit must be provided")
    if title == "":
        raise ValueError("pull request title cannot be empty")
    if base == "":
        raise ValueError("pull request base cannot be empty")

    args = ["pr", "edit", str(number), "--repo", f"{owner}/{repo}"]
    if title is not None:
        args.extend(["--title", title])
    if body is not None:
        args.extend(["--body-file", "-"])
    if labels_add:
        for label in labels_add:
            args.extend(["--add-label", label])
    if labels_remove:
        for label in labels_remove:
            args.extend(["--remove-label", label])
    if assignees_add:
        for assignee in assignees_add:
            args.extend(["--add-assignee", assignee])
    if assignees_remove:
        for assignee in assignees_remove:
            args.extend(["--remove-assignee", assignee])
    if base is not None:
        args.extend(["--base", base])

    async def write() -> GitHubRequestResult[Any]:
        return await run_write_with_metadata(
            app.client,
            *args,
            json_output=False,
            stdin_text=body if body is not None else None,
        )

    async def readback() -> dict[str, Any]:
        fields = ["title", "url"]
        if body is not None:
            fields.append("body")
        if base is not None:
            fields.append("baseRefName")
        if labels_add or labels_remove:
            fields.append("labels")
        if assignees_add or assignees_remove:
            fields.append("assignees")
        value = await app.client.run(
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            ",".join(fields),
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub returned a non-object pull request edit readback")
        return value

    def matches(value: dict[str, Any]) -> bool:
        if title is not None and value.get("title") != title:
            return False
        if body is not None and str(value.get("body") or "") != body:
            return False
        if base is not None and value.get("baseRefName") != base:
            return False
        current_labels = _names(value.get("labels"), "name")
        if labels_add and not set(labels_add).issubset(current_labels):
            return False
        if labels_remove and set(labels_remove) & current_labels:
            return False
        current_assignees = _names(value.get("assignees"), "login")
        if assignees_add and not set(assignees_add).issubset(current_assignees):
            return False
        if assignees_remove and set(assignees_remove) & current_assignees:
            return False
        return bool(value.get("url"))

    execution = await execute_write_readback(
        resource="Pull request edit",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    value = execution.readback_value or {}
    message = status.warning or "Pull request updated successfully."
    return PullRequestEdit(
        number=number,
        title=str(value.get("title") or title or ""),
        url=str(value.get("url") or ""),
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )
