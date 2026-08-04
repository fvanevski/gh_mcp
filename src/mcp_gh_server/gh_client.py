"""Asynchronous, noninteractive GitHub CLI subprocess execution."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .serialization import to_json_value

logger = logging.getLogger(__name__)

_MAX_GITHUB_ERROR_DETAIL_CHARS = 4_000

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


@dataclass(slots=True)
class GhClient:
    """Run ``gh`` without a terminal and parse its structured output."""

    settings: Any  # Settings (typed via import to avoid circular import)

    async def run(
        self,
        *args: str,
        expected_returncode: int | set[int] = 0,
        json_output: bool = True,
        stdin_text: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Execute a noninteractive ``gh`` command.

        Standard input is closed unless ``stdin_text`` is explicitly supplied.
        This prevents a child process from consuming MCP protocol input.
        """

        cmd = ["gh", *args]
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
            raise RuntimeError(
                f"gh command timed out after {command_timeout:g}s: {rendered}"
            ) from exc
        except asyncio.CancelledError:
            await _terminate_process(process)
            raise

        duration_ms = (perf_counter() - started) * 1000
        stdout = stdout_bytes.decode(errors="replace").strip()
        stderr = stderr_bytes.decode(errors="replace").strip()

        expected_returncodes = (
            expected_returncode if isinstance(expected_returncode, set) else {expected_returncode}
        )
        if process.returncode not in expected_returncodes:
            github_detail = _github_error_detail(stdout)
            detail_suffix = f"; GitHub response: {github_detail}" if github_detail else ""
            raise RuntimeError(
                f"gh command failed (exit {process.returncode}): "
                f"{stderr or 'no stderr output'}{detail_suffix}"
            )
        if process.returncode != 0 and not stdout:
            raise RuntimeError(
                f"gh command returned status {process.returncode} without structured output: "
                f"{stderr or 'no stderr output'}"
            )

        if not json_output:
            return {"stdout": stdout, "_duration_ms": duration_ms}

        if not stdout:
            return {}

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh returned non-JSON output: {stdout[:200]}") from exc

        return _normalize(data, duration_ms)

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
