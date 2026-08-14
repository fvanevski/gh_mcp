"""Shared helpers for frozen 0.6.x write-schema compatibility adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from .gh_client import GhClient
from .request_governor import GitHubRequestError, GitHubRequestResult
from .tooling import api_json_write
from .write_contracts import (
    WriteExecution,
    execute_write_readback,
    run_api_json_write_with_metadata,
)


def _looks_transport_ambiguous(error: RuntimeError) -> bool:
    """Recognize transport-like failures only for lightweight protocol fakes.

    Real ``GhClient`` executions already raise ``GitHubRequestError`` with an
    authoritative ambiguity classification. The fallback exists because historical
    compatibility tests use lightweight clients whose ``run`` or ``run_with_metadata``
    methods can surface only bare ``RuntimeError`` instances.
    """

    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "timeout",
            "timed out",
            "connection reset",
            "transport reset",
            "unexpected eof",
        )
    )


def _raise_ambiguous_fake_error(error: RuntimeError) -> None:
    """Normalize a legacy fake transport failure without affecting real ``GhClient``."""

    if _looks_transport_ambiguous(error):
        raise GitHubRequestError(
            str(error),
            retryable=True,
            ambiguous=True,
        ) from error
    raise error


async def run_write_with_metadata(
    client: GhClient,
    *args: str,
    **kwargs: Any,
) -> GitHubRequestResult[Any]:
    """Run one governed write while preserving request metadata."""

    runner = getattr(client, "run_with_metadata", None)
    if callable(runner):
        if isinstance(client, GhClient):
            return await client.run_with_metadata(*args, **kwargs)
        try:
            return await runner(*args, **kwargs)
        except GitHubRequestError:
            raise
        except RuntimeError as exc:
            _raise_ambiguous_fake_error(exc)
    try:
        value = await client.run(*args, **kwargs)
    except GitHubRequestError:
        raise
    except RuntimeError as exc:
        _raise_ambiguous_fake_error(exc)
    return GitHubRequestResult(value=value)


async def run_json_write_with_metadata(
    client: GhClient,
    method: str,
    endpoint: str,
    payload: dict[str, Any],
) -> GitHubRequestResult[Any]:
    """Send one governed JSON mutation without discarding request metadata."""

    runner = getattr(client, "run_with_metadata", None)
    if callable(runner):
        if isinstance(client, GhClient):
            return await run_api_json_write_with_metadata(client, method, endpoint, payload)
        try:
            return await run_api_json_write_with_metadata(client, method, endpoint, payload)
        except GitHubRequestError:
            raise
        except RuntimeError as exc:
            _raise_ambiguous_fake_error(exc)
    try:
        value = await api_json_write(client, method, endpoint, payload)
    except GitHubRequestError:
        raise
    except RuntimeError as exc:
        _raise_ambiguous_fake_error(exc)
    return GitHubRequestResult(value=value)


async def execute_atomic_write_readback[TWrite, TRead](
    *,
    resource: str,
    write: Callable[[], Awaitable[GitHubRequestResult[TWrite]]],
    readback: Callable[[], Awaitable[TRead]],
    state_matches_requested: Callable[[TRead], bool],
) -> WriteExecution[TWrite, TRead]:
    """Execute a write whose exact precondition is encoded server-side."""

    execution = await execute_write_readback(
        resource=resource,
        write=write,
        readback=readback,
        state_matches_requested=state_matches_requested,
    )
    return replace(
        execution,
        outcome=execution.outcome.model_copy(update={"precondition_checked": True}),
    )


def raise_known_unapplied(execution: WriteExecution[Any, Any]) -> None:
    """Re-raise a known failed write unless readback proves requested state."""

    outcome = execution.outcome
    if (
        execution.error is not None
        and outcome.write_completed is False
        and outcome.state_matches_requested is not True
    ):
        raise execution.error
