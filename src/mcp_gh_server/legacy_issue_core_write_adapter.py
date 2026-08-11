"""0.6.x compatibility adapters for issue create/edit writes."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context

from .legacy_assignee_support import resolve_assignee_groups
from .legacy_write_support import raise_known_unapplied, run_write_with_metadata
from .models import IssueCreate, IssueEdit
from .request_governor import GitHubRequestResult
from .tooling import (
    AppContext,
    app_from_context,
    optional_created_url,
    require_write_enabled,
    trailing_number,
)
from .write_contracts import execute_write_readback, legacy_write_status


def _label_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item.get("name"))
        for item in value
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _assignee_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item.get("login"))
        for item in value
        if isinstance(item, dict) and isinstance(item.get("login"), str)
    }


async def gh_create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    *,
    ctx: Context[AppContext],
) -> IssueCreate:
    """Create a new issue in a repository."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="issue_create")
    (expected_assignees,) = await resolve_assignee_groups(app.client, assignees)
    args = [
        "issue",
        "create",
        "--repo",
        f"{owner}/{repo}",
        "--title",
        title,
        "--body-file",
        "-",
    ]
    if labels:
        for label in labels:
            args.extend(["--label", label])
    if assignees:
        for assignee in assignees:
            args.extend(["--assignee", assignee])

    created_url: str | None = None

    async def write() -> GitHubRequestResult[Any]:
        nonlocal created_url
        result = await run_write_with_metadata(
            app.client,
            *args,
            json_output=False,
            stdin_text=body or "",
        )
        created_url = optional_created_url(result.value)
        return result

    async def readback() -> dict[str, Any]:
        if created_url is None:
            raise RuntimeError("issue creation returned no stable URL for readback")
        fields = ["title", "number", "url"]
        if body is not None:
            fields.append("body")
        if labels:
            fields.append("labels")
        if assignees:
            fields.append("assignees")
        result = await app.client.run(
            "issue",
            "view",
            created_url,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            ",".join(fields),
        )
        if not isinstance(result, dict):
            raise RuntimeError("GitHub returned a non-object issue readback")
        return result

    def matches(result: dict[str, Any]) -> bool:
        if result.get("title") != title:
            return False
        if body is not None and str(result.get("body") or "") != body:
            return False
        if labels and not set(labels).issubset(_label_names(result.get("labels"))):
            return False
        if expected_assignees and not expected_assignees.issubset(
            _assignee_names(result.get("assignees"))
        ):
            return False
        return isinstance(result.get("number"), int) and bool(result.get("url"))

    execution = await execute_write_readback(
        resource="Issue creation",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    result = execution.readback_value or {}
    url = str(result.get("url") or created_url or "")
    number = result.get("number")
    issue_number = number if isinstance(number, int) else trailing_number(created_url)
    message = status.warning or "Issue created successfully."
    return IssueCreate(
        number=issue_number,
        title=str(result.get("title") or title),
        url=url,
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )


async def gh_edit_issue(
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
    milestone: int | None = None,
    remove_milestone: bool = False,
) -> IssueEdit:
    """Edit an existing issue in a repository."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="issue_edit")
    if milestone is not None and remove_milestone:
        raise ValueError("milestone and remove_milestone are mutually exclusive")
    if not any(
        (
            title is not None,
            body is not None,
            labels_add,
            labels_remove,
            assignees_add,
            assignees_remove,
            milestone is not None,
            remove_milestone,
        )
    ):
        raise ValueError("at least one issue edit must be provided")
    if title == "":
        raise ValueError("issue title cannot be empty")

    expected_assignees_add, expected_assignees_remove = await resolve_assignee_groups(
        app.client,
        assignees_add,
        assignees_remove,
    )

    args = ["issue", "edit", str(number), "--repo", f"{owner}/{repo}"]
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
    if milestone is not None:
        milestone_result = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/milestones/{milestone}",
        )
        milestone_title = (
            milestone_result.get("title") if isinstance(milestone_result, dict) else None
        )
        if not isinstance(milestone_title, str) or not milestone_title:
            raise RuntimeError(f"Unable to resolve milestone #{milestone} to its title")
        args.extend(["--milestone", milestone_title])
    if remove_milestone:
        args.append("--remove-milestone")

    async def write() -> GitHubRequestResult[Any]:
        return await run_write_with_metadata(
            app.client,
            *args,
            json_output=False,
            stdin_text=body if body is not None else None,
        )

    async def readback() -> dict[str, Any]:
        fields = ["title", "number", "state", "url"]
        if body is not None:
            fields.append("body")
        if labels_add or labels_remove:
            fields.append("labels")
        if assignees_add or assignees_remove:
            fields.append("assignees")
        if milestone is not None or remove_milestone:
            fields.append("milestone")
        result = await app.client.run(
            "issue",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            ",".join(fields),
        )
        if not isinstance(result, dict):
            raise RuntimeError("GitHub returned a non-object issue edit readback")
        return result

    def matches(result: dict[str, Any]) -> bool:
        if title is not None and result.get("title") != title:
            return False
        if body is not None and str(result.get("body") or "") != body:
            return False
        current_labels = _label_names(result.get("labels"))
        if labels_add and not set(labels_add).issubset(current_labels):
            return False
        if labels_remove and set(labels_remove) & current_labels:
            return False
        current_assignees = _assignee_names(result.get("assignees"))
        if expected_assignees_add and not expected_assignees_add.issubset(current_assignees):
            return False
        if expected_assignees_remove & current_assignees:
            return False
        current_milestone = result.get("milestone")
        current_number = (
            current_milestone.get("number") if isinstance(current_milestone, dict) else None
        )
        if milestone is not None and current_number != milestone:
            return False
        if remove_milestone and current_milestone is not None:
            return False
        return result.get("number") == number

    execution = await execute_write_readback(
        resource="Issue edit",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    raise_known_unapplied(execution)
    status = legacy_write_status(execution.outcome)
    result = execution.readback_value or {}
    message = status.warning or "Issue edited successfully."
    return IssueEdit(
        number=number,
        title=str(result.get("title") or title or ""),
        state=str(result.get("state") or ""),
        url=str(result.get("url") or ""),
        write_completed=status.write_completed,
        readback_completed=status.readback_completed,
        warning=status.warning,
        message=message,
    )
