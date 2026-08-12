"""Subprocess-boundary tests for governed raw-byte GitHub evidence reads."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_gh_server.binary_evidence import stream_governed_bytes
from mcp_gh_server.gh_client import GhClient
from mcp_gh_server.request_governor import GitHubRequestGovernor
from mcp_gh_server.settings import Settings


def _fake_gh(tmp_path: Path, source: str) -> Path:
    executable = tmp_path / "gh"
    executable.write_text(f"#!/usr/bin/env python3\n{source}")
    executable.chmod(0o700)
    return executable


@pytest.mark.asyncio
async def test_stream_governed_bytes_preserves_non_utf8_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(tmp_path, "import os\nos.write(1, b'PK\\x00\\xffpayload')\n")
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
    assert metadata.attempts == 1


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_stream_governed_bytes_never_retries_after_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = tmp_path / "counter"
    _fake_gh(
        tmp_path,
        f"""from pathlib import Path
import os, sys
counter = Path({str(counter)!r})
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
os.write(1, b"partial")
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
