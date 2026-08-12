"""Unit tests for shared GitHub request governance."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from mcp_gh_server.gh_client import GhClient, _infer_request_kind, _prepare_command_args
from mcp_gh_server.request_governor import (
    READ_REQUEST,
    WRITE_REQUEST,
    GitHubRequestError,
    GitHubRequestGovernor,
    GitHubRequestKind,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.settings import Settings


class _FakeClock:
    def __init__(self) -> None:
        self.monotonic_value = 0.0
        self.wall_value = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> float:
        return self.wall_value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.monotonic_value += seconds
        self.wall_value += seconds

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall_value += seconds


def _governor(clock: _FakeClock, **kwargs: Any) -> GitHubRequestGovernor:
    return GitHubRequestGovernor(
        monotonic_clock=clock.monotonic,
        wall_clock=clock.wall,
        sleep=clock.sleep,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_safe_read_retries_transient_failures_with_bounded_backoff() -> None:
    clock = _FakeClock()
    governor = _governor(clock, max_read_attempts=3, backoff_base_seconds=0.25)
    calls = 0

    async def operation() -> GitHubRequestResult[dict[str, bool]]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise GitHubRequestError("transient", retryable=True)
        return GitHubRequestResult(
            value={"ok": True},
            metadata=GitHubRequestMetadata(request_id="req-3"),
        )

    result = await governor.execute(READ_REQUEST, operation)

    assert result.value == {"ok": True}
    assert result.metadata.request_id == "req-3"
    assert result.metadata.attempts == 3
    assert result.metadata.warning is not None
    assert "after 3 attempts" in result.metadata.warning
    assert calls == 3
    assert clock.sleeps == [0.25, 0.5]


@pytest.mark.asyncio
async def test_retry_after_stops_current_request_and_blocks_following_requests() -> None:
    clock = _FakeClock()
    governor = _governor(clock)
    calls = 0

    async def limited() -> GitHubRequestResult[None]:
        nonlocal calls
        calls += 1
        raise GitHubRequestError(
            "secondary rate limit",
            retryable=True,
            rate_limited=True,
            metadata=GitHubRequestMetadata(retry_after_seconds=7.0, request_id="req-limit"),
        )

    with pytest.raises(GitHubRequestError, match="secondary rate limit"):
        await governor.execute(READ_REQUEST, limited)
    assert calls == 1
    assert clock.sleeps == []

    blocked_called = False

    async def blocked() -> GitHubRequestResult[None]:
        nonlocal blocked_called
        blocked_called = True
        return GitHubRequestResult(value=None)

    with pytest.raises(GitHubRequestError, match="paused by a prior rate-limit") as raised:
        await governor.execute(READ_REQUEST, blocked)
    assert not blocked_called
    assert raised.value.metadata.request_id == "req-limit"
    assert raised.value.metadata.retry_after_seconds == pytest.approx(7.0)

    clock.advance(7.0)
    assert (await governor.execute(READ_REQUEST, blocked)).value is None
    assert blocked_called


@pytest.mark.asyncio
async def test_headerless_rate_limit_uses_policy_fallback_cooldown() -> None:
    clock = _FakeClock()
    governor = _governor(clock, rate_limit_fallback_seconds=60.0)

    async def limited() -> GitHubRequestResult[None]:
        raise GitHubRequestError(
            "secondary rate limit",
            rate_limited=True,
            metadata=GitHubRequestMetadata(request_id="req-fallback"),
        )

    with pytest.raises(GitHubRequestError, match="secondary rate limit"):
        await governor.execute(READ_REQUEST, limited)

    blocked_called = False

    async def blocked() -> GitHubRequestResult[str]:
        nonlocal blocked_called
        blocked_called = True
        return GitHubRequestResult(value="ok")

    with pytest.raises(GitHubRequestError, match="paused by a prior rate-limit") as raised:
        await governor.execute(READ_REQUEST, blocked)
    assert not blocked_called
    assert raised.value.metadata.request_id == "req-fallback"
    assert raised.value.metadata.retry_after_seconds == pytest.approx(60.0)

    clock.advance(59.0)
    with pytest.raises(GitHubRequestError) as still_blocked:
        await governor.execute(READ_REQUEST, blocked)
    assert still_blocked.value.metadata.retry_after_seconds == pytest.approx(1.0)

    clock.advance(1.0)
    assert (await governor.execute(READ_REQUEST, blocked)).value == "ok"
    assert blocked_called


@pytest.mark.asyncio
async def test_primary_reset_metadata_blocks_after_remaining_reaches_zero() -> None:
    clock = _FakeClock()
    governor = _governor(clock)
    reset_epoch = int(clock.wall_value + 11)

    async def exhausted() -> GitHubRequestResult[str]:
        return GitHubRequestResult(
            value="ok",
            metadata=GitHubRequestMetadata(
                rate_limit_remaining=0,
                rate_limit_reset_epoch=reset_epoch,
            ),
        )

    result = await governor.execute(READ_REQUEST, exhausted)
    assert result.value == "ok"
    assert result.metadata.warning is not None
    assert "primary rate limit is exhausted" in result.metadata.warning

    async def next_request() -> GitHubRequestResult[str]:
        return GitHubRequestResult(value="next")

    with pytest.raises(GitHubRequestError, match="paused by a prior rate-limit"):
        await governor.execute(READ_REQUEST, next_request)

    clock.advance(11)
    assert (await governor.execute(READ_REQUEST, next_request)).value == "next"


@pytest.mark.asyncio
async def test_writes_are_spaced_at_least_one_second_after_prior_write_finishes() -> None:
    clock = _FakeClock()
    governor = _governor(clock)

    async def write() -> GitHubRequestResult[str]:
        return GitHubRequestResult(value="done")

    await governor.execute(WRITE_REQUEST, write)
    await governor.execute(WRITE_REQUEST, write)

    assert clock.sleeps == [pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_writes_are_strictly_serialized() -> None:
    clock = _FakeClock()
    governor = _governor(clock)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    active = 0
    max_active = 0

    async def first() -> GitHubRequestResult[str]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        first_started.set()
        await release_first.wait()
        active -= 1
        return GitHubRequestResult(value="first")

    async def second() -> GitHubRequestResult[str]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        second_started.set()
        active -= 1
        return GitHubRequestResult(value="second")

    first_task = asyncio.create_task(governor.execute(WRITE_REQUEST, first))
    await first_started.wait()
    second_task = asyncio.create_task(governor.execute(WRITE_REQUEST, second))
    await asyncio.sleep(0)
    assert not second_started.is_set()

    release_first.set()
    assert (await first_task).value == "first"
    assert (await second_task).value == "second"
    assert max_active == 1


@pytest.mark.asyncio
async def test_cancelled_write_still_establishes_spacing() -> None:
    clock = _FakeClock()
    governor = _governor(clock)

    async def cancelled() -> GitHubRequestResult[None]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await governor.execute(WRITE_REQUEST, cancelled)

    async def next_write() -> GitHubRequestResult[str]:
        return GitHubRequestResult(value="next")

    assert (await governor.execute(WRITE_REQUEST, next_write)).value == "next"
    assert clock.sleeps == [pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_ambiguous_write_is_never_retried() -> None:
    clock = _FakeClock()
    governor = _governor(clock)
    calls = 0

    async def ambiguous() -> GitHubRequestResult[None]:
        nonlocal calls
        calls += 1
        raise GitHubRequestError(
            "transport failed after mutation may have been sent",
            retryable=True,
            ambiguous=True,
        )

    with pytest.raises(GitHubRequestError) as raised:
        await governor.execute(WRITE_REQUEST, ambiguous)

    assert raised.value.ambiguous
    assert calls == 1
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_rate_limit_is_not_treated_as_safe_read_retry() -> None:
    clock = _FakeClock()
    governor = _governor(clock)
    calls = 0

    async def limited() -> GitHubRequestResult[None]:
        nonlocal calls
        calls += 1
        raise GitHubRequestError("rate limited", retryable=True, rate_limited=True)

    with pytest.raises(GitHubRequestError, match="rate limited"):
        await governor.execute(READ_REQUEST, limited)

    assert calls == 1
    assert clock.sleeps == []


def test_write_spacing_cannot_be_configured_below_github_guidance() -> None:
    with pytest.raises(ValueError, match=r"at least 1.0"):
        GitHubRequestGovernor(write_spacing_seconds=0.999)


def test_rate_limit_fallback_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        GitHubRequestGovernor(rate_limit_fallback_seconds=0)


def test_gh_subprocess_boundary_has_no_parallel_source_bypass() -> None:
    package = Path(__file__).parents[1] / "src" / "mcp_gh_server"
    subprocess_boundaries = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "create_subprocess_exec" in path.read_text()
    }
    assert subprocess_boundaries == {"gh_client.py", "binary_evidence.py"}


def _fake_gh(tmp_path: Path, source: str) -> Path:
    executable = tmp_path / "gh"
    executable.write_text(f"#!/usr/bin/env python3\n{source}")
    executable.chmod(0o700)
    return executable


def test_request_classification_fails_closed_for_mutations_and_unknown_commands() -> None:
    assert _infer_request_kind(("issue", "view", "1")) is GitHubRequestKind.READ
    assert _infer_request_kind(("issue", "create")) is GitHubRequestKind.WRITE
    assert (
        _infer_request_kind(("api", "repos/octo/repo/contents/file", "-X", "GET", "-f", "ref=main"))
        is GitHubRequestKind.READ
    )
    assert (
        _infer_request_kind(("api", "repos/octo/repo/issues", "-f", "title=example"))
        is GitHubRequestKind.WRITE
    )
    assert _infer_request_kind(("api", "graphql")) is GitHubRequestKind.WRITE
    assert (
        _infer_request_kind(("api", "--hostname", "github.com", "graphql"))
        is GitHubRequestKind.WRITE
    )
    assert _infer_request_kind(("extension", "custom")) is GitHubRequestKind.WRITE


@pytest.mark.parametrize(
    "args",
    [
        ("api", "repos/octo/repo/issues", "--input", "payload.json"),
        ("api", "repos/octo/repo/issues", "--input=payload.json"),
        ("api", "repos/octo/repo/issues", "--field", "title=example"),
        ("api", "repos/octo/repo/issues", "--field=title=example"),
        ("api", "repos/octo/repo/issues", "--raw-field", "title=example"),
        ("api", "repos/octo/repo/issues", "--raw-field=title=example"),
        ("api", "repos/octo/repo/issues", "-Ftitle=example"),
        ("api", "repos/octo/repo/issues", "-ftitle=example"),
    ],
)
def test_body_bearing_api_forms_without_explicit_method_are_writes(
    args: tuple[str, ...],
) -> None:
    assert _infer_request_kind(args) is GitHubRequestKind.WRITE


def test_explicit_api_get_overrides_implicit_post_defaults() -> None:
    assert (
        _infer_request_kind(
            ("api", "repos/octo/repo/issues", "-X", "GET", "--input", "payload.json")
        )
        is GitHubRequestKind.READ
    )


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ("api", "repos/octo/repo", "-X", "GET"),
            ["api", "repos/octo/repo", "--include", "-X", "GET"],
        ),
        (
            ("api", "-X", "GET", "search/issues"),
            ["api", "-X", "GET", "search/issues", "--include"],
        ),
        (
            ("api", "--hostname", "github.com", "repos/octo/repo"),
            ["api", "--hostname", "github.com", "repos/octo/repo", "--include"],
        ),
    ],
)
def test_api_include_injection_preserves_supported_argument_order(
    args: tuple[str, ...],
    expected: list[str],
) -> None:
    prepared, parse_headers = _prepare_command_args(
        args,
        conditional_etag=None,
        conditional_last_modified=None,
    )
    assert parse_headers
    assert prepared == expected


@pytest.mark.asyncio
async def test_api_run_with_metadata_captures_request_and_rate_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        """import json
print("HTTP/2.0 200 OK")
print("X-GitHub-Request-Id: req-123")
print("X-RateLimit-Remaining: 42")
print("X-RateLimit-Reset: 2000000000")
print('ETag: "etag-1"')
print("Last-Modified: Mon, 10 Aug 2026 00:00:00 GMT")
print()
print(json.dumps({"ok": True}))
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    result = await client.run_with_metadata("api", "rate_limit", "-X", "GET")

    assert result.value["ok"] is True
    assert result.metadata.request_id == "req-123"
    assert result.metadata.rate_limit_remaining == 42
    assert result.metadata.rate_limit_reset_epoch == 2_000_000_000
    assert result.metadata.etag == '"etag-1"'
    assert result.metadata.last_modified == "Mon, 10 Aug 2026 00:00:00 GMT"


@pytest.mark.asyncio
async def test_api_conditional_read_handles_not_modified_without_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        """import sys
assert "--include" in sys.argv
assert 'If-None-Match: "etag-1"' in sys.argv
print("HTTP/2.0 304 Not Modified")
print("X-GitHub-Request-Id: req-304")
print('ETag: "etag-1"')
print()
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    result = await client.run_with_metadata(
        "api",
        "repos/octo/repo",
        "-X",
        "GET",
        conditional_etag='"etag-1"',
    )

    assert result.value is None
    assert result.metadata.not_modified
    assert result.metadata.request_id == "req-304"
    assert result.metadata.etag == '"etag-1"'


@pytest.mark.asyncio
async def test_api_rate_limit_surfaces_retry_after_and_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count_path = tmp_path / "count"
    _fake_gh(
        tmp_path,
        f"""from pathlib import Path
import sys
path = Path({str(count_path)!r})
count = int(path.read_text()) + 1 if path.exists() else 1
path.write_text(str(count))
print("HTTP/2.0 429 Too Many Requests")
print("X-GitHub-Request-Id: req-limit")
print("Retry-After: 5")
print()
print('{{"message": "secondary rate limit", "status": "429"}}')
print("gh: HTTP 429", file=sys.stderr)
raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with pytest.raises(GitHubRequestError, match="rate limit detected") as raised:
        await client.run("api", "repos/octo/repo", "-X", "GET")

    assert raised.value.rate_limited
    assert raised.value.metadata.request_id == "req-limit"
    assert raised.value.metadata.retry_after_seconds == 5.0
    assert count_path.read_text() == "1"


@pytest.mark.asyncio
async def test_headerless_cli_rate_limit_blocks_next_request_without_reexecuting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count_path = tmp_path / "count"
    _fake_gh(
        tmp_path,
        f"""from pathlib import Path
import sys
path = Path({str(count_path)!r})
count = int(path.read_text()) + 1 if path.exists() else 1
path.write_text(str(count))
print("secondary rate limit", file=sys.stderr)
raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with pytest.raises(GitHubRequestError, match="rate limit detected"):
        await client.run("repo", "view", "octo/repo", "--json", "nameWithOwner")
    with pytest.raises(GitHubRequestError, match="paused by a prior rate-limit"):
        await client.run("repo", "view", "octo/repo", "--json", "nameWithOwner")

    assert count_path.read_text() == "1"
