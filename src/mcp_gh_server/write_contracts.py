"""Shared exact-state precondition and write/readback contracts."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .gh_client import GhClient
from .request_governor import GitHubRequestError, GitHubRequestResult


class WritePreconditionMismatch(RuntimeError):
    """Expected GitHub state did not match immediately before a mutation."""


@dataclass(frozen=True, slots=True)
class WritePrecondition[T]:
    """One typed expected-vs-current state check performed before mutation."""

    label: str
    expected: T
    actual: T
    matches: bool


class WriteOutcomeMetadata(BaseModel):
    """Shared tri-state outcome for an exact-state GitHub mutation."""

    precondition_checked: bool
    write_completed: bool | None
    readback_completed: bool
    state_matches_requested: bool | None
    warning: str | None = None
    request_id: str | None = None


class ExactWriteResult(WriteOutcomeMetadata):
    """Required result base for new nontrivial 0.7.x write tools."""


@dataclass(frozen=True, slots=True)
class WriteExecution[TWrite, TRead]:
    """Internal mutation/readback values plus their standardized outcome."""

    outcome: WriteOutcomeMetadata
    write_value: TWrite | None = None
    readback_value: TRead | None = None
    error: RuntimeError | None = None


@dataclass(frozen=True, slots=True)
class LegacyWriteStatus:
    """Lossy projection of the tri-state contract onto the 0.6.x public schema."""

    write_completed: bool
    readback_completed: bool
    warning: str | None


def make_write_outcome(
    *,
    resource: str,
    precondition_checked: bool,
    write_completed: bool | None,
    readback_completed: bool,
    state_matches_requested: bool | None,
    warning: str | None = None,
    request_id: str | None = None,
) -> WriteOutcomeMetadata:
    """Construct outcome metadata and refuse internally inconsistent readback state."""

    if readback_completed != (state_matches_requested is not None):
        raise ValueError(
            "state_matches_requested must be set exactly when authoritative readback completed"
        )
    if state_matches_requested is False:
        warning = combine_warnings(warning, semantic_mismatch_warning(resource))
    return WriteOutcomeMetadata(
        precondition_checked=precondition_checked,
        write_completed=write_completed,
        readback_completed=readback_completed,
        state_matches_requested=state_matches_requested,
        warning=warning,
        request_id=request_id,
    )


def write_error_outcome(
    resource: str,
    *,
    precondition_checked: bool,
    error: RuntimeError,
    readback_completed: bool,
    state_matches_requested: bool | None,
) -> WriteOutcomeMetadata:
    """Classify one failed mutation attempt without replaying it."""

    request_id: str | None = None
    metadata_warning: str | None = None
    ambiguous = False
    if isinstance(error, GitHubRequestError):
        request_id = error.metadata.request_id
        metadata_warning = error.metadata.warning
        ambiguous = error.ambiguous
    warning = combine_warnings(
        metadata_warning,
        _write_error_warning(
            resource,
            ambiguous=ambiguous,
            readback_failed=not readback_completed,
            state_matches_requested=state_matches_requested,
        ),
    )
    return make_write_outcome(
        resource=resource,
        precondition_checked=precondition_checked,
        write_completed=None if ambiguous else False,
        readback_completed=readback_completed,
        state_matches_requested=state_matches_requested,
        warning=warning,
        request_id=request_id,
    )


async def require_write_precondition[T](
    read_current: Callable[[], Awaitable[T]],
    expected: T,
    *,
    label: str,
    matches: Callable[[T, T], bool] | None = None,
) -> WritePrecondition[T]:
    """Read authoritative state and fail before mutation when it does not match."""

    actual = await read_current()
    matched = actual == expected if matches is None else matches(actual, expected)
    check = WritePrecondition(label=label, expected=expected, actual=actual, matches=matched)
    if not matched:
        raise WritePreconditionMismatch(
            f"{label} precondition mismatch: expected {expected!r}, current {actual!r}; "
            "no write was attempted"
        )
    return check


async def execute_write_readback[TWrite, TRead](
    *,
    resource: str,
    write: Callable[[], Awaitable[GitHubRequestResult[TWrite]]],
    readback: Callable[[], Awaitable[TRead]],
    state_matches_requested: Callable[[TRead], bool],
    precondition: Callable[[], Awaitable[WritePrecondition[Any]]] | None = None,
) -> WriteExecution[TWrite, TRead]:
    """Execute one mutation exactly once, then semantically verify authoritative state.

    The optional precondition runs immediately before the mutation callback. The
    mutation callback is never replayed. A transport-ambiguous write remains
    ``write_completed=None`` even when a subsequent readback establishes final state.
    """

    precondition_checked = False
    if precondition is not None:
        check = await precondition()
        if not check.matches:  # Defensive: public helper already rejects mismatches.
            raise WritePreconditionMismatch(
                f"{check.label} precondition did not match; no write was attempted"
            )
        precondition_checked = True

    try:
        write_result = await write()
    except RuntimeError as exc:
        metadata_request_id: str | None = None
        metadata_warning: str | None = None
        ambiguous = False
        if isinstance(exc, GitHubRequestError):
            metadata_request_id = exc.metadata.request_id
            metadata_warning = exc.metadata.warning
            ambiguous = exc.ambiguous

        write_completed: bool | None = None if ambiguous else False
        try:
            readback_value = await readback()
        except RuntimeError:
            warning = combine_warnings(
                metadata_warning,
                _write_error_warning(resource, ambiguous=ambiguous, readback_failed=True),
            )
            return WriteExecution(
                outcome=WriteOutcomeMetadata(
                    precondition_checked=precondition_checked,
                    write_completed=write_completed,
                    readback_completed=False,
                    state_matches_requested=None,
                    warning=warning,
                    request_id=metadata_request_id,
                ),
                error=exc,
            )

        matches_requested = state_matches_requested(readback_value)
        warning = combine_warnings(
            metadata_warning,
            _write_error_warning(
                resource,
                ambiguous=ambiguous,
                readback_failed=False,
                state_matches_requested=matches_requested,
            ),
        )
        return WriteExecution(
            outcome=WriteOutcomeMetadata(
                precondition_checked=precondition_checked,
                write_completed=write_completed,
                readback_completed=True,
                state_matches_requested=matches_requested,
                warning=warning,
                request_id=metadata_request_id,
            ),
            readback_value=readback_value,
            error=exc,
        )

    request_id = write_result.metadata.request_id
    try:
        readback_value = await readback()
    except RuntimeError:
        return WriteExecution(
            outcome=WriteOutcomeMetadata(
                precondition_checked=precondition_checked,
                write_completed=True,
                readback_completed=False,
                state_matches_requested=None,
                warning=combine_warnings(
                    write_result.metadata.warning,
                    readback_failure_warning(resource),
                ),
                request_id=request_id,
            ),
            write_value=write_result.value,
        )

    matches_requested = state_matches_requested(readback_value)
    warning = write_result.metadata.warning
    if not matches_requested:
        warning = combine_warnings(warning, semantic_mismatch_warning(resource))
    return WriteExecution(
        outcome=WriteOutcomeMetadata(
            precondition_checked=precondition_checked,
            write_completed=True,
            readback_completed=True,
            state_matches_requested=matches_requested,
            warning=warning,
            request_id=request_id,
        ),
        write_value=write_result.value,
        readback_value=readback_value,
    )


def legacy_write_status(outcome: WriteOutcomeMetadata) -> LegacyWriteStatus:
    """Project the exact contract without claiming unknown/mismatched state as success."""

    warning = outcome.warning
    if outcome.request_id is not None:
        warning = combine_warnings(warning, f"GitHub request id: {outcome.request_id}.")
    return LegacyWriteStatus(
        write_completed=outcome.write_completed is True,
        readback_completed=(
            outcome.readback_completed and outcome.state_matches_requested is not False
        ),
        warning=warning,
    )


def readback_failure_warning(resource: str, locator: str | None = None) -> str:
    """Return the stable legacy-compatible warning for failed authoritative readback."""

    location = f" at {locator}" if locator else ""
    return (
        f"{resource} write completed{location}, but structured readback failed. "
        "Do not retry automatically; verify the resource first."
    )


def semantic_mismatch_warning(resource: str) -> str:
    """Warn that a completed readback did not verify the requested semantic state."""

    return (
        f"{resource} authoritative readback completed, but the resulting state does not "
        "match the requested state. Do not retry automatically; re-read authoritative "
        "state first."
    )


async def run_api_json_write_with_metadata(
    client: GhClient,
    method: str,
    endpoint: str,
    payload: dict[str, Any],
) -> GitHubRequestResult[Any]:
    """Send one governed JSON API mutation while retaining transport metadata."""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as file:
        json.dump(payload, file)
        file.flush()
        payload_path = file.name
    try:
        return await client.run_with_metadata(
            "api",
            endpoint,
            "-X",
            method,
            "--input",
            payload_path,
        )
    finally:
        os.unlink(payload_path)


def _write_error_warning(
    resource: str,
    *,
    ambiguous: bool,
    readback_failed: bool,
    state_matches_requested: bool | None = None,
) -> str:
    if ambiguous:
        if readback_failed:
            return (
                f"{resource} write transport outcome is unknown and authoritative readback "
                "also failed. Do not retry automatically; re-read authoritative state first."
            )
        if state_matches_requested:
            return (
                f"{resource} write transport outcome is unknown, but authoritative readback "
                "matches the requested state. Do not retry the mutation."
            )
        return (
            f"{resource} write transport outcome is unknown and authoritative readback does "
            "not match the requested state. Do not retry automatically; re-read authoritative "
            "state first."
        )

    if readback_failed:
        return (
            f"{resource} write failed before confirmed completion and authoritative readback "
            "also failed. Re-read authoritative state before deciding whether to retry."
        )
    if state_matches_requested:
        return (
            f"{resource} write failed before confirmed completion, but authoritative readback "
            "already matches the requested state. Do not retry the mutation."
        )
    return (
        f"{resource} write failed and authoritative readback does not match the requested "
        "state. The mutation was not retried."
    )


def combine_warnings(*warnings: str | None) -> str | None:
    parts = [warning.strip() for warning in warnings if warning and warning.strip()]
    return " ".join(parts) or None
