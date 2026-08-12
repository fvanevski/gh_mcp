"""Asynchronous, noninteractive GitHub CLI subprocess execution."""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import re
import shlex
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .request_governor import (
    READ_REQUEST,
    WRITE_REQUEST,
    GitHubRequestError,
    GitHubRequestGovernor,
    GitHubRequestKind,
    GitHubRequestMetadata,
    GitHubRequestPolicy,
    GitHubRequestResult,
)
from .serialization import to_json_value

logger = logging.getLogger(__name__)

_MAX_GITHUB_ERROR_DETAIL_CHARS = 4_000
_STREAM_CHUNK_BYTES = 64 * 1024
_STREAM_STDERR_RETAIN_BYTES = 64 * 1024
_HTTP_STATUS_RE = re.compile(r"\bHTTP(?:/\S+)?\s+(\d{3})\b", re.IGNORECASE)
_HTTP_ERROR_STATUS_RE = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)
_GRAPHQL_OPERATION_RE = re.compile(
    r"^\s*(?:#[^\r\n]*(?:\r?\n|$)\s*)*(query|mutation|subscription)\b",
    re.IGNORECASE,
)
_GRAPHQL_NON_QUERY_OPERATION_RE = re.compile(r"\b(?:mutation|subscription)\b", re.IGNORECASE)
_TRANSPORT_FAILURE_MARKERS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "context deadline exceeded",
    "i/o timeout",
    "network is unreachable",
    "no such host",
    "temporary failure",
    "tls handshake timeout",
    "unexpected eof",
)
_RATE_LIMIT_MARKERS = (
    "api rate limit exceeded",
    "secondary rate limit",
    "rate limit exceeded",
    "abuse detection",
)

_ENV_OVERRIDES = {
    "GH_PROMPT_DISABLED": "1",
    "GH_PAGER": "cat",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
    "GH_SPINNER_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
}

_READ_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "auth": frozenset({"status"}),
    "issue": frozenset({"list", "status", "view"}),
    "label": frozenset({"list"}),
    "pr": frozenset({"checks", "diff", "list", "status", "view"}),
    "release": frozenset({"list", "view"}),
    "repo": frozenset({"list", "view"}),
    "run": frozenset({"list", "view", "watch"}),
    "search": frozenset({"code", "commits", "issues", "repos"}),
    "workflow": frozenset({"list", "view"}),
}
_API_BODY_FLAGS = frozenset({"-F", "--field", "-f", "--raw-field", "--input"})
_API_FLAGS_WITH_VALUE = frozenset(
    {
        "--cache",
        "-F",
        "--field",
        "-H",
        "--header",
        "--hostname",
        "--input",
        "-q",
        "--jq",
        "-X",
        "--method",
        "-p",
        "--preview",
        "-f",
        "--raw-field",
        "-t",
        "--template",
    }
)
_API_BOOLEAN_FLAGS = frozenset(
    {"-i", "--include", "--paginate", "--silent", "--slurp", "--verbose"}
)
_STREAM_FORBIDDEN_FLAGS = frozenset({"-i", "--include", "--paginate", "--slurp"})


@dataclass(frozen=True, slots=True)
class _IncludedResponse:
    body: str
    status_code: int | None
    headers: dict[str, str]


@dataclass(slots=True)
class GhClient:
    """Run ``gh`` without a terminal and parse its structured output."""

    settings: Any  # Settings (typed via import to avoid circular import)
    governor: GitHubRequestGovernor = field(default_factory=GitHubRequestGovernor)

    async def run(
        self,
        *args: str,
        expected_returncode: int | set[int] = 0,
        json_output: bool = True,
        stdin_text: str | None = None,
        timeout: float | None = None,
        conditional_etag: str | None = None,
        conditional_last_modified: str | None = None,
    ) -> Any:
        """Execute a governed, noninteractive ``gh`` command and return its payload."""

        result = await self.run_with_metadata(
            *args,
            expected_returncode=expected_returncode,
            json_output=json_output,
            stdin_text=stdin_text,
            timeout=timeout,
            conditional_etag=conditional_etag,
            conditional_last_modified=conditional_last_modified,
        )
        return result.value

    async def run_with_metadata(
        self,
        *args: str,
        expected_returncode: int | set[int] = 0,
        json_output: bool = True,
        stdin_text: str | None = None,
        timeout: float | None = None,
        conditional_etag: str | None = None,
        conditional_last_modified: str | None = None,
    ) -> GitHubRequestResult[Any]:
        """Execute ``gh`` and retain request identity/rate-limit metadata."""

        policy = _request_policy(args)

        async def attempt() -> GitHubRequestResult[Any]:
            return await self._run_once(
                *args,
                expected_returncode=expected_returncode,
                json_output=json_output,
                stdin_text=stdin_text,
                timeout=timeout,
                policy=policy,
                conditional_etag=conditional_etag,
                conditional_last_modified=conditional_last_modified,
            )

        return await self.governor.execute(policy, attempt)

    async def stream_text(
        self,
        *args: str,
        on_chunk: Callable[[str], None],
        timeout: float | None = None,
    ) -> GitHubRequestMetadata:
        """Stream one governed read-only ``gh`` response into a synchronous text sink.

        Streaming reads intentionally disable transparent retries because ``on_chunk`` may
        already have consumed evidence from a failed attempt. The operation still enters
        the shared governor for serialization and rate-limit state.
        """

        if _infer_request_kind(args) is not GitHubRequestKind.READ:
            raise ValueError("stream_text accepts only commands classified as read-only")
        if any(flag in _STREAM_FORBIDDEN_FLAGS for flag in args):
            raise ValueError("stream_text does not support include/paginate/slurp output framing")

        policy = GitHubRequestPolicy(kind=GitHubRequestKind.READ, retry_safe=False)

        async def attempt() -> GitHubRequestResult[None]:
            return await self._stream_text_once(
                *args,
                on_chunk=on_chunk,
                timeout=timeout,
                policy=policy,
            )

        return (await self.governor.execute(policy, attempt)).metadata

    async def _stream_text_once(
        self,
        *args: str,
        on_chunk: Callable[[str], None],
        timeout: float | None,
        policy: GitHubRequestPolicy,
    ) -> GitHubRequestResult[None]:
        """Execute one read command while incrementally decoding stdout."""

        cmd = ["gh", *args]
        rendered = _redacted_command(cmd)
        logger.debug("Running streaming gh command: %s", rendered)
        command_timeout = timeout if timeout is not None else self.settings.command_timeout_seconds
        env = self._environment()

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=os.name == "posix",
        )
        stdout = process.stdout
        raw_stderr = process.stderr
        assert stdout is not None
        assert raw_stderr is not None
        stderr_task = asyncio.create_task(_drain_bounded(raw_stderr, _STREAM_STDERR_RETAIN_BYTES))

        async def consume_stdout() -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                chunk = await stdout.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    on_chunk(text)
            final_text = decoder.decode(b"", final=True)
            if final_text:
                on_chunk(final_text)
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
                f"gh command failed (exit {process.returncode}): {stderr or 'no stderr output'}"
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

    async def _run_once(
        self,
        *args: str,
        expected_returncode: int | set[int],
        json_output: bool,
        stdin_text: str | None,
        timeout: float | None,
        policy: GitHubRequestPolicy,
        conditional_etag: str | None,
        conditional_last_modified: str | None,
    ) -> GitHubRequestResult[Any]:
        """Execute one subprocess attempt; callers must enter through the governor."""

        prepared_args, parse_headers = _prepare_command_args(
            args,
            conditional_etag=conditional_etag,
            conditional_last_modified=conditional_last_modified,
        )
        cmd = ["gh", *prepared_args]
        rendered = _redacted_command(cmd)
        logger.debug("Running gh command: %s", rendered)
        started = perf_counter()
        command_timeout = timeout if timeout is not None else self.settings.command_timeout_seconds
        env = self._environment()

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=(
                asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=os.name == "posix",
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(None if stdin_text is None else stdin_text.encode()),
                timeout=command_timeout,
            )
        except TimeoutError as exc:
            await _terminate_process(process)
            message = f"gh command timed out after {command_timeout:g}s: {rendered}"
            ambiguous = policy.kind is GitHubRequestKind.WRITE
            if ambiguous:
                message += (
                    "; write outcome may be ambiguous; re-read authoritative state before retrying"
                )
            raise GitHubRequestError(
                message,
                retryable=True,
                ambiguous=ambiguous,
            ) from exc
        except asyncio.CancelledError:
            await _terminate_process(process)
            raise

        duration_ms = (perf_counter() - started) * 1000
        raw_stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace").strip()
        included = (
            _split_included_response(raw_stdout)
            if parse_headers
            else _IncludedResponse(body=raw_stdout, status_code=None, headers={})
        )
        stdout = included.body.strip()
        status_code = included.status_code or _status_from_stderr(stderr)
        metadata = _metadata_from_headers(
            included.headers,
            not_modified=included.status_code == 304,
        )

        expected_returncodes = (
            expected_returncode if isinstance(expected_returncode, set) else {expected_returncode}
        )
        conditional_request = conditional_etag is not None or conditional_last_modified is not None
        if included.status_code == 304 and conditional_request:
            return GitHubRequestResult(value=None, metadata=metadata)

        if process.returncode not in expected_returncodes:
            github_detail = _github_error_detail(stdout)
            detail_suffix = f"; GitHub response: {github_detail}" if github_detail else ""
            rate_limited = _is_rate_limited(status_code, stdout, stderr, metadata)
            retryable = _is_retryable_failure(status_code, stderr)
            ambiguous = (
                policy.kind is GitHubRequestKind.WRITE
                and not rate_limited
                and (status_code is None or retryable)
            )
            message = (
                f"gh command failed (exit {process.returncode}): "
                f"{stderr or 'no stderr output'}{detail_suffix}"
            )
            if rate_limited:
                message += (
                    "; GitHub rate limit detected; retries are stopped and further requests "
                    "remain blocked until the applicable reset, retry, or fallback cooldown"
                )
            elif ambiguous:
                message += (
                    "; write outcome may be ambiguous; re-read authoritative state before retrying"
                )
            raise GitHubRequestError(
                message,
                status_code=status_code,
                retryable=retryable,
                ambiguous=ambiguous,
                rate_limited=rate_limited,
                metadata=metadata,
            )
        if process.returncode != 0 and not stdout:
            raise GitHubRequestError(
                f"gh command returned status {process.returncode} without structured output: "
                f"{stderr or 'no stderr output'}",
                status_code=status_code,
                metadata=metadata,
            )

        if not json_output:
            return GitHubRequestResult(
                value={"stdout": stdout, "_duration_ms": duration_ms},
                metadata=metadata,
            )

        if not stdout:
            return GitHubRequestResult(value={}, metadata=metadata)

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GitHubRequestError(
                f"gh returned non-JSON output: {stdout[:200]}",
                status_code=status_code,
                metadata=metadata,
            ) from exc

        return GitHubRequestResult(value=_normalize(data, duration_ms), metadata=metadata)

    def clamp_max_results(self, requested: int | None) -> int:
        """Clamp per_page to configured limits."""

        value = requested if requested is not None else self.settings.default_max_results
        if value < 0:
            raise ValueError("per_page must be zero or greater")
        return int(min(value, self.settings.hard_max_results))

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(_ENV_OVERRIDES)
        if self.settings.github_token is not None:
            env["GH_TOKEN"] = self.settings.github_token.get_secret_value()
        return env


def _request_policy(args: tuple[str, ...]) -> GitHubRequestPolicy:
    return READ_REQUEST if _infer_request_kind(args) is GitHubRequestKind.READ else WRITE_REQUEST


def _infer_request_kind(args: tuple[str, ...]) -> GitHubRequestKind:
    """Classify proven reads; unknown or potentially mutating commands fail closed."""

    if not args:
        return GitHubRequestKind.WRITE
    command = args[0]
    if command == "api":
        method = _api_method(args)
        if method in {"GET", "HEAD"}:
            return GitHubRequestKind.READ
        if method == "POST" and _graphql_document_is_read_only_query(args):
            return GitHubRequestKind.READ
        return GitHubRequestKind.WRITE
    if command == "version":
        return GitHubRequestKind.READ
    if len(args) >= 2 and args[1] in _READ_SUBCOMMANDS.get(command, frozenset()):
        return GitHubRequestKind.READ
    return GitHubRequestKind.WRITE


def _api_endpoint_index(args: tuple[str, ...]) -> int | None:
    """Locate the API endpoint without assuming that it precedes supported flags."""

    index = 1
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return index + 1 if index + 1 < len(args) else None
        if arg in _API_FLAGS_WITH_VALUE:
            if index + 1 >= len(args):
                return None
            index += 2
            continue
        if arg in _API_BOOLEAN_FLAGS:
            index += 1
            continue
        if arg.startswith("-"):
            if "=" in arg or (len(arg) > 2 and not arg.startswith("--")):
                index += 1
                continue
            return None
        return index
    return None


def _api_field_values(args: tuple[str, ...], field_name: str) -> list[str]:
    """Return explicit gh-api field values without reading indirect input files."""

    values: list[str] = []
    index = 1
    while index < len(args):
        arg = args[index]
        raw_field: str | None = None
        if arg in {"-F", "--field", "-f", "--raw-field"}:
            if index + 1 >= len(args):
                return []
            raw_field = args[index + 1]
            index += 2
        elif arg.startswith("--field=") or arg.startswith("--raw-field="):
            raw_field = arg.split("=", 1)[1]
            index += 1
        elif (arg.startswith("-F") and arg != "-F") or (arg.startswith("-f") and arg != "-f"):
            raw_field = arg[2:]
            index += 1
        else:
            index += 1
            continue

        key, separator, value = raw_field.partition("=")
        if separator and key == field_name:
            values.append(value)
    return values


def _graphql_document_is_read_only_query(args: tuple[str, ...]) -> bool:
    """Recognize only explicit GraphQL query documents; ambiguous forms fail closed."""

    endpoint_index = _api_endpoint_index(args)
    if endpoint_index is None or args[endpoint_index].casefold() != "graphql":
        return False
    if any(arg == "--input" or arg.startswith("--input=") for arg in args):
        return False

    query_values = _api_field_values(args, "query")
    if len(query_values) != 1:
        return False
    document = query_values[0]
    operation = _GRAPHQL_OPERATION_RE.match(document)
    if operation is None or operation.group(1).casefold() != "query":
        return False

    # False negatives are intentional: if a query document contains a token that could
    # denote a mutation/subscription operation, retain conservative write semantics.
    remainder = document[operation.end() :]
    return _GRAPHQL_NON_QUERY_OPERATION_RE.search(remainder) is None


def _api_method(args: tuple[str, ...]) -> str:
    endpoint_index = _api_endpoint_index(args)
    default_method = (
        "POST" if endpoint_index is None or args[endpoint_index].casefold() == "graphql" else "GET"
    )

    explicit_method: str | None = None
    has_body = False
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in {"-X", "--method"}:
            if index + 1 < len(args):
                explicit_method = args[index + 1]
                index += 2
                continue
        elif arg.startswith("--method="):
            explicit_method = arg.split("=", 1)[1]
        elif arg in _API_BODY_FLAGS:
            has_body = True
            index += 2
            continue
        elif arg.startswith(("--field=", "--raw-field=", "--input=")) or any(
            arg.startswith(prefix) and arg != prefix for prefix in ("-F", "-f")
        ):
            has_body = True
        elif arg in _API_FLAGS_WITH_VALUE:
            index += 2
            continue
        index += 1

    if explicit_method:
        return explicit_method.upper()
    if has_body:
        return "POST"
    return default_method


def _prepare_command_args(
    args: tuple[str, ...],
    *,
    conditional_etag: str | None,
    conditional_last_modified: str | None,
) -> tuple[list[str], bool]:
    conditional_request = conditional_etag is not None or conditional_last_modified is not None
    is_api = bool(args) and args[0] == "api"
    paginated = "--paginate" in args or "--slurp" in args
    parse_headers = is_api and ("--include" in args or "-i" in args)

    if conditional_request:
        if not is_api or _api_method(args) not in {"GET", "HEAD"}:
            raise ValueError("conditional headers are supported only for read-only gh api requests")
        if paginated:
            raise ValueError("conditional headers are not supported with gh api pagination")
        _validate_header_value(conditional_etag, "conditional_etag")
        _validate_header_value(conditional_last_modified, "conditional_last_modified")

    prepared = list(args)
    if is_api and not paginated and not parse_headers:
        endpoint_index = _api_endpoint_index(args)
        insert_at = endpoint_index + 1 if endpoint_index is not None else len(prepared)
        prepared.insert(insert_at, "--include")
        parse_headers = True
    if conditional_etag is not None:
        prepared.extend(["-H", f"If-None-Match: {conditional_etag}"])
    if conditional_last_modified is not None:
        prepared.extend(["-H", f"If-Modified-Since: {conditional_last_modified}"])
    return prepared, parse_headers


def _validate_header_value(value: str | None, name: str) -> None:
    if value is not None and ("\r" in value or "\n" in value):
        raise ValueError(f"{name} must not contain line breaks")


def _split_included_response(stdout: str) -> _IncludedResponse:
    remaining = stdout
    status_code: int | None = None
    headers: dict[str, str] = {}

    while remaining.startswith("HTTP/"):
        boundary, separator_length = _header_boundary(remaining)
        if boundary is None:
            break
        block = remaining[:boundary]
        remaining = remaining[boundary + separator_length :]
        lines = block.splitlines()
        if not lines:
            break
        match = _HTTP_STATUS_RE.search(lines[0])
        if match is None:
            break
        status_code = int(match.group(1))
        headers = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().casefold()] = value.strip()
        if not remaining.startswith("HTTP/"):
            break

    return _IncludedResponse(body=remaining, status_code=status_code, headers=headers)


def _header_boundary(value: str) -> tuple[int | None, int]:
    crlf = value.find("\r\n\r\n")
    lf = value.find("\n\n")
    valid = [(position, length) for position, length in ((crlf, 4), (lf, 2)) if position >= 0]
    if not valid:
        return None, 0
    return min(valid, key=lambda item: item[0])


def _metadata_from_headers(
    headers: Mapping[str, str],
    *,
    not_modified: bool,
) -> GitHubRequestMetadata:
    return GitHubRequestMetadata(
        request_id=headers.get("x-github-request-id"),
        retry_after_seconds=_nonnegative_float(headers.get("retry-after")),
        rate_limit_reset_epoch=_nonnegative_int(headers.get("x-ratelimit-reset")),
        rate_limit_remaining=_nonnegative_int(headers.get("x-ratelimit-remaining")),
        etag=headers.get("etag"),
        last_modified=headers.get("last-modified"),
        not_modified=not_modified,
    )


def _nonnegative_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _nonnegative_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _status_from_stderr(stderr: str) -> int | None:
    match = _HTTP_ERROR_STATUS_RE.search(stderr)
    return int(match.group(1)) if match else None


def _is_rate_limited(
    status_code: int | None,
    stdout: str,
    stderr: str,
    metadata: GitHubRequestMetadata,
) -> bool:
    if status_code == 429:
        return True
    if status_code == 403 and metadata.rate_limit_remaining == 0:
        return True
    detail = f"{stderr}\n{stdout}".casefold()
    return any(marker in detail for marker in _RATE_LIMIT_MARKERS)


def _is_retryable_failure(status_code: int | None, stderr: str) -> bool:
    if status_code in {408, 500, 502, 503, 504}:
        return True
    if status_code is not None:
        return False
    detail = stderr.casefold()
    return any(marker in detail for marker in _TRANSPORT_FAILURE_MARKERS)


def _github_error_detail(stdout: str) -> str | None:
    """Extract a bounded, non-secret validation summary from a GitHub JSON error body."""

    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    detail: dict[str, Any] = {}
    for key in ("message", "documentation_url", "status"):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            detail[key] = value

    errors = payload.get("errors")
    if isinstance(errors, list):
        safe_errors: list[str | dict[str, str | int]] = []
        for error in errors[:20]:
            if isinstance(error, str):
                safe_errors.append(error[:1_000])
            elif isinstance(error, dict):
                safe_error = {
                    key: value
                    for key in ("resource", "field", "code", "message")
                    if isinstance((value := error.get(key)), (str, int))
                }
                if safe_error:
                    safe_errors.append(safe_error)
        if safe_errors:
            detail["errors"] = safe_errors
    elif isinstance(errors, str):
        detail["errors"] = errors[:1_000]

    if not detail:
        return None
    rendered = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= _MAX_GITHUB_ERROR_DETAIL_CHARS:
        return rendered
    return rendered[: _MAX_GITHUB_ERROR_DETAIL_CHARS - 1] + "…"


async def _drain_bounded(
    stream: asyncio.StreamReader,
    retain_bytes: int,
) -> bytes:
    """Drain a stream completely while retaining at most its first bounded bytes."""

    retained = bytearray()
    while True:
        chunk = await stream.read(_STREAM_CHUNK_BYTES)
        if not chunk:
            break
        remaining = retain_bytes - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
    return bytes(retained)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a command and its descendants after timeout or cancellation."""

    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        return
    except TimeoutError:
        pass

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    await process.wait()


def _redacted_command(cmd: list[str]) -> str:
    """Render a command without logging user-authored content."""

    redacted = [
        value if index < 3 or value.startswith("-") else "<redacted>"
        for index, value in enumerate(cmd)
    ]
    return shlex.join(redacted)


def _normalize(value: Any, duration_ms: float) -> Any:
    """Normalize gh JSON output to JSON-safe values, adding timing metadata."""

    if isinstance(value, Mapping):
        normalized = {str(k): to_json_value(v) for k, v in value.items()}
        normalized["_duration_ms"] = duration_ms
        return normalized

    if isinstance(value, list):
        return [to_json_value(item) for item in value]

    return to_json_value(value)
