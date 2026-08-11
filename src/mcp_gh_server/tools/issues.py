"""Issue, label, milestone, and issue-comment tools."""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any

from mcp.server.mcpserver import Context

from ..models import (
    CommentCreate,
    IssueCreate,
    IssueEdit,
    IssueInfo,
    LabelCreate,
    LabelEdit,
    MilestoneCreate,
    SearchResults,
)
from ..tooling import (
    ADD_EXTERNAL,
    MUTATE_EXTERNAL,
    READ_EXTERNAL,
    AppContext,
    app_from_context,
    created_json,
    get_label,
    mcp,
    optional_created_url,
    readback_warning,
    require_write_enabled,
    trailing_number,
)


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_list_issues(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    state: str = "all",
    per_page: int | None = None,
    labels: str | None = None,
) -> SearchResults:
    """List issues in a repository.

    state: open, closed, or all (default: all).
    labels: comma-separated label filter.
    """

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = "title,url,number,state,author,body,createdAt,updatedAt,closedAt,labels,comments"
    args = [
        "issue",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--state",
        state,
        "--limit",
        str(limit),
    ]
    if labels:
        args.extend(["--labels", labels])

    result = await app.client.run(*args)
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} ({state})",
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_get_issue(
    owner: str,
    repo: str,
    number: int,
    *,
    ctx: Context[AppContext],
) -> IssueInfo:
    """Get details of a specific issue or pull request, including its body."""

    app = app_from_context(ctx)
    fields = (
        "title,url,number,state,author,body,createdAt,updatedAt,closedAt,labels,comments,milestone"
    )
    result = await app.client.run(
        "issue",
        "view",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
    )
    author_obj = result.get("author")
    if isinstance(author_obj, dict):
        result["author"] = author_obj.get("login")
    return IssueInfo.model_validate(result)


@mcp.tool(annotations=ADD_EXTERNAL)
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

    create_result = await app.client.run(*args, json_output=False, stdin_text=body or "")
    created_url = optional_created_url(create_result)
    if created_url is None:
        warning = readback_warning("Issue")
        return IssueCreate(
            number=0,
            title=title,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    try:
        result = await app.client.run(
            "issue",
            "view",
            created_url,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "title,number,url",
        )
    except RuntimeError:
        warning = readback_warning("Issue", created_url)
        return IssueCreate(
            number=trailing_number(created_url),
            title=title,
            url=created_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return IssueCreate(
        number=result.get("number", 0),
        title=result.get("title", title),
        url=result.get("url", ""),
        message="Issue created successfully.",
    )


@mcp.tool(annotations=MUTATE_EXTERNAL)
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

    args = [
        "issue",
        "edit",
        str(number),
        "--repo",
        f"{owner}/{repo}",
    ]
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
        milestone_title = milestone_result.get("title")
        if not isinstance(milestone_title, str) or not milestone_title:
            raise RuntimeError(f"Unable to resolve milestone #{milestone} to its title")
        args.extend(["--milestone", milestone_title])
    if remove_milestone:
        args.append("--remove-milestone")

    await app.client.run(*args, json_output=False, stdin_text=body if body is not None else None)
    try:
        result = await app.client.run(
            "issue",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "title,number,state,url",
        )
    except RuntimeError:
        warning = readback_warning("Issue edit", f"{owner}/{repo}#{number}")
        return IssueEdit(
            number=number,
            title=title or "",
            state="",
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return IssueEdit(
        number=result.get("number", number),
        title=result.get("title", ""),
        state=result.get("state", ""),
        url=result.get("url", ""),
        message="Issue edited successfully.",
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_list_labels(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    per_page: int | None = None,
) -> SearchResults:
    """List labels in a repository."""

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = "name,color,description,createdAt,updatedAt,url,isDefault"
    result = await app.client.run(
        "label",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--limit",
        str(limit),
    )
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} labels",
    )


@mcp.tool(annotations=ADD_EXTERNAL)
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
    return await _create_label(app, owner, repo, name, color, description, force=False)


@mcp.tool(annotations=MUTATE_EXTERNAL)
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
    return await _create_label(app, owner, repo, name, color, description, force=True)


async def _create_label(
    app: AppContext,
    owner: str,
    repo: str,
    name: str,
    color: str,
    description: str | None,
    *,
    force: bool,
) -> LabelCreate:
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        raise ValueError("label color must be exactly six hexadecimal characters")

    args = [
        "label",
        "create",
        name,
        "--repo",
        f"{owner}/{repo}",
        "--color",
        color,
    ]
    if description is not None:
        args.extend(["--description", description])
    if force:
        args.append("--force")

    await app.client.run(*args, json_output=False)
    try:
        result = await get_label(app.client, owner, repo, name)
    except RuntimeError:
        warning = readback_warning("Label", name)
        return LabelCreate(
            name=name,
            color=color,
            description=description,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return LabelCreate(
        name=result.get("name", name),
        color=result.get("color", color),
        description=result.get("description", description),
        url=result.get("url", ""),
        message="Label created successfully.",
    )


@mcp.tool(annotations=MUTATE_EXTERNAL)
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

    args = [
        "label",
        "edit",
        name,
        "--repo",
        f"{owner}/{repo}",
    ]
    if new_name is not None:
        args.extend(["--name", new_name])
    if color is not None:
        args.extend(["--color", color])
    if description is not None:
        args.extend(["--description", description])

    await app.client.run(*args, json_output=False)
    result_name = new_name or name
    try:
        result = await get_label(app.client, owner, repo, result_name)
    except RuntimeError:
        warning = readback_warning("Label edit", result_name)
        return LabelEdit(
            name=result_name,
            color=color or "",
            description=description,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return LabelEdit(
        name=result.get("name", name),
        color=result.get("color", ""),
        description=result.get("description", ""),
        url=result.get("url", ""),
        message="Label edited successfully.",
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_list_milestones(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    state: str = "all",
    per_page: int | None = None,
) -> SearchResults:
    """List milestones in a repository via the GitHub API."""

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/milestones",
        "-X",
        "GET",
        "-f",
        f"per_page={limit}",
        "-f",
        f"state={state}",
    )
    items: list[Any]
    if isinstance(result, dict) and "stdout" in result:
        raw = result["stdout"]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        items = parsed if isinstance(parsed, list) else []
    elif isinstance(result, list):
        items = result
    else:
        items = []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} milestones ({state})",
    )


@mcp.tool(annotations=ADD_EXTERNAL)
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

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as file:
        json.dump(payload, file)
        file.flush()
        payload_path = file.name

    try:
        create_result = await app.client.run(
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
        created = created_json(create_result, "Milestone")
    except RuntimeError:
        warning = readback_warning("Milestone")
        return MilestoneCreate(
            number=0,
            title=title,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    number = created.get("number")
    if not isinstance(number, int):
        warning = readback_warning("Milestone")
        return MilestoneCreate(
            number=0,
            title=title,
            url=str(created.get("url", "")),
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    try:
        result = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/milestones/{number}",
        )
    except RuntimeError:
        warning = readback_warning("Milestone", f"#{number}")
        return MilestoneCreate(
            number=number,
            title=str(created.get("title", title)),
            url=str(created.get("url", "")),
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    return MilestoneCreate(
        number=result.get("number", number),
        title=result.get("title", title),
        url=result.get("url", ""),
        message="Milestone created successfully.",
    )


@mcp.tool(annotations=ADD_EXTERNAL)
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
    create_result = await app.client.run(
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
    created_url = optional_created_url(create_result)
    if created_url is None:
        warning = readback_warning("Comment")
        return CommentCreate(
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return CommentCreate(
        url=created_url,
        message="Comment posted successfully.",
    )
