"""Host-bounded public schema facade for canonical issue-domain writes."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import Context
from pydantic import Field

from .issue_write_models import (
    IssueCreateResult,
    IssueEditResult,
    LabelCreateResult,
    LabelEditResult,
    MilestoneCreateResult,
)
from .tooling import AppContext
from .tools.issue_writes import (
    gh_create_issue as _gh_create_issue,
    gh_create_label as _gh_create_label,
    gh_create_milestone as _gh_create_milestone,
    gh_edit_issue as _gh_edit_issue,
    gh_edit_label as _gh_edit_label,
)
from .write_tool_schema import (
    Assignees,
    Body,
    Description,
    DueOn,
    LabelColor,
    LabelName,
    Labels,
    Owner,
    PositiveNumber,
    Repository,
    Title,
)


async def gh_create_issue(
    owner: Owner,
    repo: Repository,
    title: Annotated[Title, Field(description="Issue title.")],
    body: Annotated[
        Body | None,
        Field(description="Optional Markdown issue body."),
    ] = None,
    labels: Annotated[
        Labels | None,
        Field(description="Optional labels to apply."),
    ] = None,
    assignees: Annotated[
        Assignees | None,
        Field(description="Optional GitHub user logins or the @me selector to assign."),
    ] = None,
    *,
    ctx: Context[AppContext],
) -> IssueCreateResult:
    return await _gh_create_issue(owner, repo, title, body, labels, assignees, ctx=ctx)


async def gh_edit_issue(
    owner: Owner,
    repo: Repository,
    number: Annotated[PositiveNumber, Field(description="Issue number to edit.")],
    *,
    ctx: Context[AppContext],
    title: Annotated[Title | None, Field(description="Replacement issue title.")] = None,
    body: Annotated[Body | None, Field(description="Replacement Markdown issue body.")] = None,
    labels_add: Annotated[Labels | None, Field(description="Labels to add.")] = None,
    labels_remove: Annotated[Labels | None, Field(description="Labels to remove.")] = None,
    assignees_add: Annotated[
        Assignees | None,
        Field(description="Assignee logins or the @me selector to add."),
    ] = None,
    assignees_remove: Annotated[
        Assignees | None,
        Field(description="Assignee logins or the @me selector to remove."),
    ] = None,
    milestone: Annotated[PositiveNumber | None, Field(description="Milestone number to set.")] = None,
    remove_milestone: bool = False,
) -> IssueEditResult:
    return await _gh_edit_issue(
        owner,
        repo,
        number,
        ctx=ctx,
        title=title,
        body=body,
        labels_add=labels_add,
        labels_remove=labels_remove,
        assignees_add=assignees_add,
        assignees_remove=assignees_remove,
        milestone=milestone,
        remove_milestone=remove_milestone,
    )


async def gh_create_label(
    owner: Owner,
    repo: Repository,
    name: Annotated[LabelName, Field(description="New label name.")],
    color: Annotated[LabelColor, Field(description="Six-character hexadecimal label color.")],
    *,
    ctx: Context[AppContext],
    description: Annotated[
        Description | None,
        Field(description="Optional label description."),
    ] = None,
) -> LabelCreateResult:
    return await _gh_create_label(owner, repo, name, color, ctx=ctx, description=description)


async def gh_edit_label(
    owner: Owner,
    repo: Repository,
    name: Annotated[LabelName, Field(description="Existing label name.")],
    *,
    ctx: Context[AppContext],
    new_name: Annotated[LabelName | None, Field(description="Replacement label name.")] = None,
    color: Annotated[
        LabelColor | None,
        Field(description="Replacement six-character hexadecimal label color."),
    ] = None,
    description: Annotated[
        Description | None,
        Field(description="Replacement label description."),
    ] = None,
) -> LabelEditResult:
    return await _gh_edit_label(
        owner,
        repo,
        name,
        ctx=ctx,
        new_name=new_name,
        color=color,
        description=description,
    )


async def gh_create_milestone(
    owner: Owner,
    repo: Repository,
    title: Annotated[Title, Field(description="Milestone title.")],
    *,
    ctx: Context[AppContext],
    description: Annotated[
        Description | None,
        Field(description="Optional milestone description."),
    ] = None,
    due_on: Annotated[
        DueOn | None,
        Field(description="Optional ISO-8601 milestone due date/time."),
    ] = None,
    state: Annotated[
        Literal["open", "closed"],
        Field(description="Initial milestone state."),
    ] = "open",
) -> MilestoneCreateResult:
    return await _gh_create_milestone(
        owner,
        repo,
        title,
        ctx=ctx,
        description=description,
        due_on=due_on,
        state=state,
    )


ISSUE_PUBLIC_WRITE_TOOLS = (
    gh_create_issue,
    gh_edit_issue,
    gh_create_label,
    gh_edit_label,
    gh_create_milestone,
)
