"""Canonical issue, label, and milestone write implementations."""

from __future__ import annotations

import re
from typing import Any

from mcp.server.mcpserver import Context

from ..issue_write_models import (
    IssueCreateResult,
    IssueEditResult,
    LabelCreateResult,
    LabelEditResult,
    MilestoneCreateResult,
)
from ..request_governor import GitHubRequestResult
from ..tooling import (
    AppContext,
    app_from_context,
    get_label,
    logger,
    optional_created_url,
    require_write_enabled,
    trailing_number,
)
from ..write_contracts import (
    WriteOutcomeMetadata,
    execute_write_readback,
    run_api_json_write_with_metadata,
)


async def _resolve_assignee_groups(
    client: Any,
    *groups: list[str] | None,
) -> tuple[set[str], ...]:
    """Resolve @me so authoritative readback compares concrete GitHub logins."""

    self_login: str | None = None
    if any(group and "@me" in group for group in groups):
        account = await client.run("api", "user")
        login = account.get("login") if isinstance(account, dict) else None
        if not isinstance(login, str) or not login:
            raise RuntimeError("Unable to resolve @me to the authenticated GitHub login")
        self_login = login

    resolved: list[set[str]] = []
    for group in groups:
        names: set[str] = set()
        for value in group or []:
            names.add(self_login if value == "@me" and self_login is not None else value)
        resolved.append(names)
    return tuple(resolved)


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


def _outcome_message(
    outcome: WriteOutcomeMetadata,
    *,
    success: str,
    unverified: str,
) -> str:
    if outcome.write_completed is True and outcome.state_matches_requested is True:
        return success
    return outcome.warning or unverified


async def gh_create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    *,
    ctx: Context[AppContext],
) -> IssueCreateResult:
    """Create one issue and semantically verify the stable created resource."""

    logger.info("MCP tool invocation reached server: tool=gh_create_issue")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="issue_create")
    (expected_assignees,) = await _resolve_assignee_groups(app.client, assignees)
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
    for label in labels or []:
        args.extend(["--label", label])
    for assignee in assignees or []:
        args.extend(["--assignee", assignee])

    created_url: str | None = None

    async def write() -> GitHubRequestResult[Any]:
        nonlocal created_url
        result = await app.client.run_with_metadata(
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
    outcome = execution.outcome
    result = execution.readback_value if isinstance(execution.readback_value, dict) else {}
    raw_number = result.get("number")
    issue_number = (
        raw_number
        if isinstance(raw_number, int)
        else trailing_number(created_url)
        if created_url is not None
        else 0
    )
    return IssueCreateResult(
        number=issue_number,
        title=str(result.get("title") or title),
        url=str(result.get("url") or created_url or ""),
        message=_outcome_message(
            outcome,
            success="Issue created and verified successfully.",
            unverified="Issue creation was not verified.",
        ),
        **outcome.model_dump(),
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
) -> IssueEditResult:
    """Edit one issue's metadata and semantically verify requested fields."""

    logger.info("MCP tool invocation reached server: tool=gh_edit_issue")
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

    expected_assignees_add, expected_assignees_remove = await _resolve_assignee_groups(
        app.client,
        assignees_add,
        assignees_remove,
    )

    args = ["issue", "edit", str(number), "--repo", f"{owner}/{repo}"]
    if title is not None:
        args.extend(["--title", title])
    if body is not None:
        args.extend(["--body-file", "-"])
    for label in labels_add or []:
        args.extend(["--add-label", label])
    for label in labels_remove or []:
        args.extend(["--remove-label", label])
    for assignee in assignees_add or []:
        args.extend(["--add-assignee", assignee])
    for assignee in assignees_remove or []:
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
        return await app.client.run_with_metadata(
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
        if result.get("number") != number:
            return False
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
        milestone_ok = milestone is None or current_number == milestone
        milestone_removal_ok = not remove_milestone or current_milestone is None
        return milestone_ok and milestone_removal_ok

    execution = await execute_write_readback(
        resource="Issue edit",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    outcome = execution.outcome
    result = execution.readback_value if isinstance(execution.readback_value, dict) else {}
    return IssueEditResult(
        number=number,
        title=str(result.get("title") or title or ""),
        state=str(result.get("state") or ""),
        url=str(result.get("url") or ""),
        message=_outcome_message(
            outcome,
            success="Issue edited and verified successfully.",
            unverified="Issue edit was not verified.",
        ),
        **outcome.model_dump(),
    )


async def _read_label(app: AppContext, owner: str, repo: str, name: str) -> dict[str, Any]:
    result = await get_label(app.client, owner, repo, name)
    if not isinstance(result, dict):
        raise RuntimeError("GitHub returned a non-object label readback")
    return result


async def gh_create_label(
    owner: str,
    repo: str,
    name: str,
    color: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
) -> LabelCreateResult:
    """Create one label without overwrite semantics and verify its exact state."""

    logger.info("MCP tool invocation reached server: tool=gh_create_label")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="label_create")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        raise ValueError("label color must be exactly six hexadecimal characters")

    args = ["label", "create", name, "--repo", f"{owner}/{repo}", "--color", color]
    if description is not None:
        args.extend(["--description", description])

    async def write() -> GitHubRequestResult[Any]:
        return await app.client.run_with_metadata(*args, json_output=False)

    async def readback() -> dict[str, Any]:
        return await _read_label(app, owner, repo, name)

    def matches(result: dict[str, Any]) -> bool:
        if str(result.get("name") or "") != name:
            return False
        if str(result.get("color") or "").casefold() != color.casefold():
            return False
        return description is None or result.get("description") == description

    execution = await execute_write_readback(
        resource="Label creation",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    outcome = execution.outcome
    result = execution.readback_value if isinstance(execution.readback_value, dict) else {}
    return LabelCreateResult(
        name=str(result.get("name") or name),
        color=str(result.get("color") or color),
        description=result.get("description", description),
        url=str(result.get("url") or ""),
        message=_outcome_message(
            outcome,
            success="Label created and verified successfully.",
            unverified="Label creation was not verified.",
        ),
        **outcome.model_dump(),
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
) -> LabelEditResult:
    """Edit one label and verify requested name, color, and description fields."""

    logger.info("MCP tool invocation reached server: tool=gh_edit_label")
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
        return await app.client.run_with_metadata(*args, json_output=False)

    async def readback() -> dict[str, Any]:
        return await _read_label(app, owner, repo, result_name)

    def matches(result: dict[str, Any]) -> bool:
        if str(result.get("name") or "") != result_name:
            return False
        if color is not None and str(result.get("color") or "").casefold() != color.casefold():
            return False
        return description is None or result.get("description") == description

    execution = await execute_write_readback(
        resource="Label edit",
        write=write,
        readback=readback,
        state_matches_requested=matches,
    )
    outcome = execution.outcome
    result = execution.readback_value if isinstance(execution.readback_value, dict) else {}
    return LabelEditResult(
        name=str(result.get("name") or result_name),
        color=str(result.get("color") or color or ""),
        description=result.get("description", description),
        url=str(result.get("url") or ""),
        message=_outcome_message(
            outcome,
            success="Label edited and verified successfully.",
            unverified="Label edit was not verified.",
        ),
        **outcome.model_dump(),
    )


async def gh_create_milestone(
    owner: str,
    repo: str,
    title: str,
    *,
    ctx: Context[AppContext],
    description: str | None = None,
    due_on: str | None = None,
    state: str = "open",
) -> MilestoneCreateResult:
    """Create one milestone and verify it by the stable returned milestone number."""

    logger.info("MCP tool invocation reached server: tool=gh_create_milestone")
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
        result = await run_api_json_write_with_metadata(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/milestones",
            payload,
        )
        created = result.value if isinstance(result.value, dict) else {}
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
    outcome = execution.outcome
    result = execution.readback_value if isinstance(execution.readback_value, dict) else created
    raw_number = result.get("number")
    final_number = raw_number if isinstance(raw_number, int) else (number or 0)
    return MilestoneCreateResult(
        number=final_number,
        title=str(result.get("title") or title),
        url=str(result.get("url") or ""),
        message=_outcome_message(
            outcome,
            success="Milestone created and verified successfully.",
            unverified="Milestone creation was not verified.",
        ),
        **outcome.model_dump(),
    )
