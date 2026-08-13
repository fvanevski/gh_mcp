"""Subprocess-boundary tests for governed raw-byte GitHub evidence reads."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_gh_server.binary_evidence import stream_governed_bytes
from mcp_gh_server.gh_client import GhClient
from mcp_gh_server.request_governor import (
    READ_REQUEST,
    GitHubRequestError,
    GitHubRequestGovernor,
    GitHubRequestResult,
)
from mcp_gh_server.settings import Settings


def _fake_gh(tmp_path: Path, source: str) -> Path:
    executable = tmp_path / "gh"
    executable.write_text(f"#!/usr/bin/env python3\n{source}")
    executable.chmod(0o700)
    return executable


def _included_response(
    body: bytes,
    *,
    status: str = "200 OK",
    headers: dict[str, str] | None = None,
) -> bytes:
    lines = [f"HTTP/1.1 {status}"]
    lines.extend(f"{key}: {value}" for key, value in (headers or {}).items())
    lines.extend(["", ""])
    return "\r\n".join(lines).encode("ascii") + body


async def test_stream_governed_bytes_preserves_non_utf8_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _included_response(
        b"PK\x00\xffpayload",
        headers={"X-GitHub-Request-Id": "req-binary"},
    )
    _fake_gh(tmp_path, f"import os\nos.write(1, {payload!r})\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())
    chunks: list[bytes] = []

    metadata = await stream_governed_bytes(
        client,
        "api",
        "repos/octo/repo/actions/artifacts/77/zip",
        "-X",
        "GET",
        on_chunk=chunks.append,
    )

    assert b"".join(chunks) == b"PK\x00\xffpayload"
    assert metadata.request_id == "req-binary"
    assert metadata.attempts == 1


async def test_stream_governed_bytes_preserves_primary_rate_limit_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _included_response(
        b"PKpayload",
        headers={
            "X-GitHub-Request-Id": "req-primary",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1011",
        },
    )
    _fake_gh(tmp_path, f"import os\nos.write(1, {payload!r})\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    governor = GitHubRequestGovernor(wall_clock=lambda: 1000.0)
    client = GhClient(Settings(), governor=governor)
    chunks: list[bytes] = []

    metadata = await stream_governed_bytes(
        client,
        "api",
        "repos/octo/repo/actions/artifacts/77/zip",
        "-X",
        "GET",
        on_chunk=chunks.append,
    )

    assert b"".join(chunks) == b"PKpayload"
    assert metadata.request_id == "req-primary"
    assert metadata.rate_limit_remaining == 0
    assert metadata.rate_limit_reset_epoch == 1011
    assert metadata.warning is not None
    assert "primary rate limit is exhausted" in metadata.warning

    async def next_read() -> GitHubRequestResult[None]:
        return GitHubRequestResult(value=None)

    with pytest.raises(GitHubRequestError, match="paused by a prior rate-limit") as blocked:
        await governor.execute(READ_REQUEST, next_read)
    assert blocked.value.metadata.retry_after_seconds == pytest.approx(11.0)


async def test_stream_governed_bytes_preserves_retry_after_on_rate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"message":"secondary rate limit"}'
    payload = _included_response(
        body,
        status="429 Too Many Requests",
        headers={
            "X-GitHub-Request-Id": "req-secondary",
            "Retry-After": "7",
        },
    )
    _fake_gh(
        tmp_path,
        f"""import os, sys
os.write(1, {payload!r})
print("gh: secondary rate limit (HTTP 429)", file=sys.stderr)
raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    governor = GitHubRequestGovernor(wall_clock=lambda: 1000.0)
    client = GhClient(Settings(), governor=governor)
    chunks: list[bytes] = []

    with pytest.raises(GitHubRequestError, match="rate limit detected") as raised:
        await stream_governed_bytes(
            client,
            "api",
            "repos/octo/repo/actions/artifacts/77/zip",
            "-X",
            "GET",
            on_chunk=chunks.append,
        )

    assert b"".join(chunks) == body
    assert raised.value.rate_limited is True
    assert raised.value.metadata.request_id == "req-secondary"
    assert raised.value.metadata.retry_after_seconds == pytest.approx(7.0)

    async def next_read() -> GitHubRequestResult[None]:
        return GitHubRequestResult(value=None)

    with pytest.raises(GitHubRequestError, match="paused by a prior rate-limit") as blocked:
        await governor.execute(READ_REQUEST, next_read)
    assert blocked.value.metadata.retry_after_seconds == pytest.approx(7.0)


async def test_stream_governed_bytes_rejects_write_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "executed"
    _fake_gh(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with pytest.raises(ValueError, match="read-only"):
        await stream_governed_bytes(
            client,
            "issue",
            "create",
            on_chunk=lambda _: None,
        )

    assert not marker.exists()


async def test_stream_governed_bytes_never_retries_after_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = tmp_path / "counter"
    payload = _included_response(b"partial")
    _fake_gh(
        tmp_path,
        f"""from pathlib import Path
import os, sys
counter = Path({str(counter)!r})
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
os.write(1, {payload!r})
print("connection reset", file=sys.stderr)
raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    governor = GitHubRequestGovernor(
        max_read_attempts=3,
        backoff_base_seconds=0,
        backoff_max_seconds=0,
    )
    client = GhClient(Settings(), governor=governor)
    chunks: list[bytes] = []

    with pytest.raises(RuntimeError, match="connection reset"):
        await stream_governed_bytes(
            client,
            "api",
            "repos/octo/repo/actions/artifacts/77/zip",
            "-X",
            "GET",
            on_chunk=chunks.append,
        )

    assert b"".join(chunks) == b"partial"
    assert counter.read_text() == "1"
