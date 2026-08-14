"""Exact-head pull-request draft-state transition tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from ..pr_draft_state_models import PullRequestDraftStateTransitionResult
from ..request_governor import GitHubRequestResult
from ..tooling import (
    OBJECT_SHA_RE,
    OWNER_RE,
    REPO_RE,
    AppContext,
    app_from_context,
    logger,
    require_write_enabled,
)
from ..write_contracts import (
    WritePrecondition,
    execute_write_readback,
    require_write_precondition,
)


@dataclass(frozen=True, slots=True)
class _PullRequestDraftSnapshot:
    number: int
    head_sha: str
    is_draft: bool
    url: str


def _parse_pr_draft_snapshot(
    raw: object,
    *,
    expected_number: int,
) -> _PullRequestDraftSnapshot:
    """Validate the exact REST pull-request fields used as transition evidence."""

    if not isinstance(raw, dict):
        raise RuntimeError("GitHub returned a non-object pull-request draft-state response")

    number = raw.get("number")
    head = raw.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    is_draft = raw.get("draft")
    url = raw.get("html_url")

    if number != expected_number:
        raise RuntimeError("GitHub pull-request draft-state readback returned a different number")
    if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub did not return a valid pull-request head SHA")
    if not isinstance(is_draft, bool):
        raise RuntimeError("GitHub did not return a valid pull-request draft state")
    if not isinstance(url, str) or not url:
        raise RuntimeError("GitHub pull-request draft-state readback returned no pull-request URL")

    return _PullRequestDraftSnapshot(
        number=number,
        head_sha=head_sha.lower(),
        is_draft=is_draft,
        url=url,
    )


async def _read_pr_draft_state(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
) -> _PullRequestDraftSnapshot:
    """Read authoritative pull-request head identity and draft state together."""

    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}",
        "-X",
        "GET",
    )
    return _parse_pr_draft_snapshot(result, expected_number=number)


def _validate_transition(expected_is_draft: bool, new_is_draft: bool) -> None:
    """Reject a request that does not describe an actual draft-state transition."""

    if expected_is_draft == new_is_draft:
        raise ValueError(
            "expected_is_draft and new_is_draft must describe an actual draft-state transition"
        )


async def gh_set_pr_draft_state(
    owner: Annotated[
        str,
        Field(
            description="GitHub repository owner or organization login.",
            min_length=1,
            max_length=39,
            pattern=OWNER_RE.pattern,
        ),
    ],
    repo: Annotated[
        str,
        Field(
            description="GitHub repository name without the owner prefix.",
            min_length=1,
            max_length=100,
            pattern=REPO_RE.pattern,
        ),
    ],
    number: Annotated[int, Field(description="Positive pull request number to transition.", ge=1)],
    expected_head_sha: Annotated[
        str,
        Field(
            description="Exact current pull-request head SHA required immediately before mutation.",
            pattern=OBJECT_SHA_RE.pattern,
        ),
    ],
    expected_is_draft: Annotated[
        bool,
        Field(description="Exact current pull-request draft state required before mutation."),
    ],
    new_is_draft: Annotated[
        bool,
        Field(description="Requested draft state: true for draft, false for ready-for-review."),
    ],
    *,
    ctx: Context[AppContext],
) -> PullRequestDraftStateTransitionResult:
    """Perform one guarded pull-request draft-state transition and verify readback."""

    logger.info("MCP tool invocation reached server: tool=gh_set_pr_draft_state")
    if number < 1:
        raise ValueError("pull request number must be positive")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_draft_state")
    _validate_transition(expected_is_draft, new_is_draft)

    expected_head = expected_head_sha.lower()
    previous_snapshot: _PullRequestDraftSnapshot | None = None

    async def read_expected_state() -> tuple[str, bool]:
        nonlocal previous_snapshot
        previous_snapshot = await _read_pr_draft_state(app, owner, repo, number)
        return previous_snapshot.head_sha, previous_snapshot.is_draft

    async def precondition() -> WritePrecondition[tuple[str, bool]]:
        return await require_write_precondition(
            read_expected_state,
            (expected_head, expected_is_draft),
            label=f"pull request {owner}/{repo}#{number} head and draft state",
        )

    async def write() -> GitHubRequestResult[Any]:
        args = [
            "pr",
            "ready",
            str(number),
            "--repo",
            f"{owner}/{repo}",
        ]
        if new_is_draft:
            args.append("--undo")
        return await app.client.run_with_metadata(*args, json_output=False)

    async def readback() -> _PullRequestDraftSnapshot:
        return await _read_pr_draft_state(app, owner, repo, number)

    execution = await execute_write_readback(
        resource=f"Pull request {owner}/{repo}#{number} draft-state transition",
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=lambda snapshot: (
            snapshot.head_sha == expected_head and snapshot.is_draft is new_is_draft
        ),
    )

    if previous_snapshot is None:
        raise RuntimeError(
            "pull-request draft-state precondition completed without an authoritative snapshot"
        )

    current = execution.readback_value
    outcome = execution.outcome
    return PullRequestDraftStateTransitionResult(
        number=number,
        previous_head_sha=previous_snapshot.head_sha,
        current_head_sha=current.head_sha if current is not None else None,
        previous_is_draft=previous_snapshot.is_draft,
        current_is_draft=current.is_draft if current is not None else None,
        url=current.url if current is not None else previous_snapshot.url,
        precondition_checked=outcome.precondition_checked,
        write_completed=outcome.write_completed,
        readback_completed=outcome.readback_completed,
        state_matches_requested=outcome.state_matches_requested,
        warning=outcome.warning,
        request_id=outcome.request_id,
    )
