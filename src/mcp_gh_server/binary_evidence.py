"""Governed binary streaming for bounded read-only GitHub evidence."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable

from .gh_client import (
    GhClient,
    _drain_bounded,
    _infer_request_kind,
    _is_rate_limited,
    _is_retryable_failure,
    _metadata_from_headers,
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
_STREAM_HEADER_BYTES = 64 * 1024
_FORBIDDEN_STREAM_FLAGS = frozenset({"-i", "--include", "--paginate", "--slurp"})
_HTTP_STATUS_LINE_RE = re.compile(r"^HTTP/\S+\s+(\d{3})\b", re.IGNORECASE)


async def stream_governed_bytes(
    client: GhClient,
    *args: str,
    on_chunk: Callable[[bytes], None],
    timeout: float | None = None,
) -> GitHubRequestMetadata:
    """Stream one governed read-only ``gh`` response as raw bytes.

    API reads internally request response headers so request/rate-limit metadata can
    reach the shared governor. That framing is consumed before body bytes reach the
    sink. Transparent retries remain disabled because the sink may already have
    observed body evidence from a failed attempt.
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


def _split_included_header(buffered: bytearray) -> tuple[int, dict[str, str], bytes] | None:
    """Split one bounded ``gh api --include`` header block from raw body bytes."""

    boundaries = [
        (position, length)
        for position, length in (
            (buffered.find(b"\r\n\r\n"), 4),
            (buffered.find(b"\n\n"), 2),
        )
        if position >= 0
    ]
    if not boundaries:
        return None

    boundary, separator_length = min(boundaries, key=lambda item: item[0])
    if boundary > _STREAM_HEADER_BYTES:
        raise RuntimeError(
            f"gh api response headers exceeded the {_STREAM_HEADER_BYTES}-byte hard limit"
        )

    header_text = bytes(buffered[:boundary]).decode("iso-8859-1", errors="replace")
    lines = header_text.splitlines()
    if not lines:
        raise RuntimeError("gh api response omitted the HTTP status line")
    status_match = _HTTP_STATUS_LINE_RE.search(lines[0])
    if status_match is None:
        raise RuntimeError("gh api response returned malformed included HTTP headers")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().casefold()] = value.strip()

    body = bytes(buffered[boundary + separator_length :])
    return int(status_match.group(1)), headers, body


async def _stream_bytes_once(
    client: GhClient,
    *args: str,
    on_chunk: Callable[[bytes], None],
    timeout: float | None,
    policy: GitHubRequestPolicy,
) -> GitHubRequestResult[None]:
    """Execute one raw-byte read attempt through the shared client environment."""

    include_headers = bool(args) and args[0] == "api"
    command_args = [*args, "--include"] if include_headers else list(args)
    cmd = ["gh", *command_args]
    rendered = _redacted_command(cmd)
    command_timeout = timeout if timeout is not None else client.settings.command_timeout_seconds

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
    stderr_task = asyncio.create_task(_drain_bounded(raw_stderr, _STREAM_STDERR_RETAIN_BYTES))

    header_buffer = bytearray()
    headers_complete = not include_headers
    response_status: int | None = None
    response_headers: dict[str, str] = {}

    async def consume_stdout() -> None:
        nonlocal headers_complete, response_status, response_headers

        while True:
            chunk = await stdout.read(64 * 1024)
            if not chunk:
                break
            if headers_complete:
                on_chunk(chunk)
                continue

            header_buffer.extend(chunk)
            parsed = _split_included_header(header_buffer)
            if parsed is None:
                if len(header_buffer) > _STREAM_HEADER_BYTES:
                    raise RuntimeError(
                        f"gh api response headers exceeded the "
                        f"{_STREAM_HEADER_BYTES}-byte hard limit"
                    )
                continue

            response_status, response_headers, body = parsed
            headers_complete = True
            header_buffer.clear()
            if body:
                on_chunk(body)

        if not headers_complete:
            raise RuntimeError("gh api response ended before included HTTP headers were complete")
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
    status_code = response_status or _status_from_stderr(stderr)
    metadata = (
        _metadata_from_headers(response_headers, not_modified=response_status == 304)
        if include_headers
        else GitHubRequestMetadata()
    )
    if process.returncode != 0:
        rate_limited = _is_rate_limited(status_code, "", stderr, metadata) or (
            status_code in {403, 429} and metadata.retry_after_seconds is not None
        )
        retryable = _is_retryable_failure(status_code, stderr)
        message = f"gh command failed (exit {process.returncode}): {stderr or 'no stderr output'}"
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
