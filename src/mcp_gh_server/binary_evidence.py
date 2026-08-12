"""Governed binary streaming for bounded read-only GitHub evidence."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable

from .gh_client import (
    GhClient,
    _drain_bounded,
    _infer_request_kind,
    _is_rate_limited,
    _is_retryable_failure,
    _redacted_command,
    _status_from_stderr,
    _terminate_process,
)
from .request_governor import (
    GitHubRequestError,
    GitHubRequestKind,
    GitHubRequestMetadata,
    GitHubRequestPolicy,
    GitHubRequestResult,
)

_STREAM_STDERR_RETAIN_BYTES = 64 * 1024
_FORBIDDEN_STREAM_FLAGS = frozenset({"-i", "--include", "--paginate", "--slurp"})


async def stream_governed_bytes(
    client: GhClient,
    *args: str,
    on_chunk: Callable[[bytes], None],
    timeout: float | None = None,
) -> GitHubRequestMetadata:
    """Stream one governed read-only ``gh`` response as raw bytes.

    The sink is synchronous by design so callers can account/hash/write each chunk
    before more evidence is consumed. Transparent retries are disabled because the
    sink may already have observed bytes from a failed attempt.
    """

    if _infer_request_kind(args) is not GitHubRequestKind.READ:
        raise ValueError("stream_governed_bytes accepts only commands classified as read-only")
    if any(flag in _FORBIDDEN_STREAM_FLAGS for flag in args):
        raise ValueError(
            "stream_governed_bytes does not support include/paginate/slurp output framing"
        )

    policy = GitHubRequestPolicy(kind=GitHubRequestKind.READ, retry_safe=False)

    async def attempt() -> GitHubRequestResult[None]:
        return await _stream_bytes_once(
            client,
            *args,
            on_chunk=on_chunk,
            timeout=timeout,
            policy=policy,
        )

    return (await client.governor.execute(policy, attempt)).metadata


async def _stream_bytes_once(
    client: GhClient,
    *args: str,
    on_chunk: Callable[[bytes], None],
    timeout: float | None,
    policy: GitHubRequestPolicy,
) -> GitHubRequestResult[None]:
    """Execute one raw-byte read attempt through the shared client environment."""

    cmd = ["gh", *args]
    rendered = _redacted_command(cmd)
    command_timeout = (
        timeout if timeout is not None else client.settings.command_timeout_seconds
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=client._environment(),
        start_new_session=os.name == "posix",
    )
    stdout = process.stdout
    raw_stderr = process.stderr
    assert stdout is not None
    assert raw_stderr is not None
    stderr_task = asyncio.create_task(
        _drain_bounded(raw_stderr, _STREAM_STDERR_RETAIN_BYTES)
    )

    async def consume_stdout() -> None:
        while True:
            chunk = await stdout.read(64 * 1024)
            if not chunk:
                break
            on_chunk(chunk)
        await process.wait()

    try:
        await asyncio.wait_for(consume_stdout(), timeout=command_timeout)
    except TimeoutError as exc:
        await _terminate_process(process)
        await stderr_task
        raise GitHubRequestError(
            f"gh command timed out after {command_timeout:g}s: {rendered}",
            retryable=True,
            ambiguous=policy.kind is GitHubRequestKind.WRITE,
        ) from exc
    except asyncio.CancelledError:
        await _terminate_process(process)
        await stderr_task
        raise
    except BaseException:
        await _terminate_process(process)
        await stderr_task
        raise

    stderr = (await stderr_task).decode(errors="replace").strip()
    status_code = _status_from_stderr(stderr)
    metadata = GitHubRequestMetadata()
    if process.returncode != 0:
        rate_limited = _is_rate_limited(status_code, "", stderr, metadata)
        retryable = _is_retryable_failure(status_code, stderr)
        message = (
            f"gh command failed (exit {process.returncode}): "
            f"{stderr or 'no stderr output'}"
        )
        if rate_limited:
            message += (
                "; GitHub rate limit detected; retries are stopped and further requests "
                "remain blocked until the applicable reset, retry, or fallback cooldown"
            )
        raise GitHubRequestError(
            message,
            status_code=status_code,
            retryable=retryable,
            ambiguous=False,
            rate_limited=rate_limited,
            metadata=metadata,
        )

    return GitHubRequestResult(value=None, metadata=metadata)
