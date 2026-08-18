"""Exact-state issue lifecycle transition tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, cast

from mcp.server.mcpserver import Context
from pydantic import Field

from ..issue_state_models import (
    IssueState,
    IssueStateReason,
    IssueStateTransitionResult,
)
from ..request_governor import GitHubRequestResult
from ..tooling import (
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
    run_api_json_write_with_metadata,
)

_CLOSED_REASONS = frozenset({"completed", "not_planned", "duplicate"})
_VALID_REASONS = _CLOSED_REASONS | {"reopened"}
_VALID_STATES = frozenset({"open", "closed"})


@dataclass(frozen=True, slots=True)
class _IssueStateSnapshot:
    number: int
    state: IssueState
    state_reason: IssueStateReason | None
    closed_at: str | None
    updated_at: str | None
    url: str


def _parse_issue_state_snapshot(raw: object, *, expected_number: int) -> _IssueStateSnapshot:
    """Validate the exact REST issue fields used as lifecycle evidence."""

    if not isinstance(raw, dict):
        raise RuntimeError("GitHub returned a non-object issue state response")
    if "pull_request" in raw:
        raise RuntimeError(
            "gh_set_issue_state accepts issues only; the requested number identifies a pull request"
        )

    number = raw.get("number")
    state = raw.get("state")
    state_reason = raw.get("state_reason")
    closed_at = raw.get("closed_at")
    updated_at = raw.get("updated_at")
    url = raw.get("html_url")

    if not isinstance(number, int) or isinstance(number, bool) or number != expected_number:
        raise RuntimeError("GitHub issue state readback returned a different issue number")
    if not isinstance(state, str) or state not in _VALID_STATES:
        raise RuntimeError("GitHub issue state readback returned an unsupported state")
    if state_reason is not None and (
        not isinstance(state_reason, str) or state_reason not in _VALID_REASONS
    ):
        raise RuntimeError("GitHub issue state readback returned an unsupported state reason")
    if closed_at is not None and not isinstance(closed_at, str):
        raise RuntimeError("GitHub issue state readback returned a malformed closed timestamp")
    if updated_at is not None and not isinstance(updated_at, str):
        raise RuntimeError("GitHub issue state readback returned a malformed updated timestamp")
    if not isinstance(url, str) or not url:
        raise RuntimeError("GitHub issue state readback returned no issue URL")

    return _IssueStateSnapshot(
        number=number,
        state=cast(IssueState, state),
        state_reason=cast(IssueStateReason | None, state_reason),
        closed_at=closed_at,
        updated_at=updated_at,
        url=url,
    )


async def _read_issue_state(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
) -> _IssueStateSnapshot:
    """Read authoritative issue lifecycle state from one fixed REST resource."""

    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/issues/{number}",
        "-X",
        "GET",
    )
    return _parse_issue_state_snapshot(result, expected_number=number)


def _write_updated_at(raw: object) -> str | None:
    """Extract the transition update timestamp from a confirmed REST mutation response."""

    if not isinstance(raw, dict):
        return None
    updated_at = raw.get("updated_at")
    return updated_at if isinstance(updated_at, str) else None


def _validate_transition(
    expected_state: IssueState,
    new_state: IssueState,
    state_reason: IssueStateReason,
) -> None:
    """Reject non-transitions and reason/state combinations GitHub cannot represent."""

    if expected_state not in _VALID_STATES or new_state not in _VALID_STATES:
        raise ValueError("expected_state and new_state must be 'open' or 'closed'")
    if state_reason not in _VALID_REASONS:
        raise ValueError("state_reason must be completed, not_planned, duplicate, or reopened")
    if expected_state == new_state:
        raise ValueError("expected_state and new_state must describe an actual state transition")
    if new_state == "closed" and state_reason not in _CLOSED_REASONS:
        raise ValueError("closing an issue requires completed, not_planned, or duplicate")
    if new_state == "open" and state_reason != "reopened":
        raise ValueError("reopening an issue requires state_reason='reopened'")


async def gh_set_issue_state(
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
    number: Annotated[int, Field(description="Positive issue number to transition.", ge=1)],
    expected_state: Annotated[
        IssueState,
        Field(description="Exact current issue state required immediately before mutation."),
    ],
    new_state: Annotated[
        IssueState,
        Field(description="Requested issue state after the transition."),
    ],
    state_reason: Annotated[
        IssueStateReason,
        Field(
            description=(
                "completed, not_planned, or duplicate when closing; reopened when reopening."
            )
        ),
    ],
    *,
    ctx: Context[AppContext],
) -> IssueStateTransitionResult:
    """Perform one guarded issue state transition and verify authoritative readback."""

    logger.info("MCP tool invocation reached server: tool=gh_set_issue_state")
    if number < 1:
        raise ValueError("issue number must be positive")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="issue_state")
    _validate_transition(expected_state, new_state, state_reason)

    endpoint = f"repos/{owner}/{repo}/issues/{number}"
    previous_snapshot: _IssueStateSnapshot | None = None

    async def read_expected_state() -> IssueState:
        nonlocal previous_snapshot
        previous_snapshot = await _read_issue_state(app, owner, repo, number)
        return previous_snapshot.state

    async def precondition() -> WritePrecondition[IssueState]:
        return await require_write_precondition(
            read_expected_state,
            expected_state,
            label=f"issue {owner}/{repo}#{number} state",
        )

    async def write() -> GitHubRequestResult[Any]:
        return await run_api_json_write_with_metadata(
            app.client,
            "PATCH",
            endpoint,
            {"state": new_state, "state_reason": state_reason},
        )

    async def readback() -> _IssueStateSnapshot:
        return await _read_issue_state(app, owner, repo, number)

    execution = await execute_write_readback(
        resource=f"Issue {owner}/{repo}#{number} state transition",
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=lambda snapshot: (
            snapshot.state == new_state and snapshot.state_reason == state_reason
        ),
    )

    if previous_snapshot is None:
        raise RuntimeError("issue state precondition completed without an authoritative snapshot")

    current = execution.readback_value
    reopened_at = None
    if current is not None and current.state == "open" and current.state_reason == "reopened":
        reopened_at = _write_updated_at(execution.write_value) or current.updated_at

    outcome = execution.outcome
    return IssueStateTransitionResult(
        number=number,
        previous_state=previous_snapshot.state,
        new_state=current.state if current is not None else None,
        state_reason=current.state_reason if current is not None else None,
        closed_at=current.closed_at if current is not None else None,
        reopened_at=reopened_at,
        url=current.url if current is not None else previous_snapshot.url,
        precondition_checked=outcome.precondition_checked,
        write_completed=outcome.write_completed,
        readback_completed=outcome.readback_completed,
        state_matches_requested=outcome.state_matches_requested,
        warning=outcome.warning,
        request_id=outcome.request_id,
    )
