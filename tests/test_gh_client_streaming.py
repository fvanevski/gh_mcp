"""Streaming subprocess-boundary tests for large read-only evidence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_gh_server.gh_client import GhClient
from mcp_gh_server.request_governor import GitHubRequestGovernor
from mcp_gh_server.settings import Settings


def _fake_gh(tmp_path: Path, source: str) -> Path:
    executable = tmp_path / "gh"
    executable.write_text(f"#!/usr/bin/env python3\n{source}")
    executable.chmod(0o700)
    return executable


@pytest.mark.asyncio
async def test_stream_text_incrementally_decodes_utf8_and_preserves_source_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(
        tmp_path,
        """import os, sys
assert sys.argv[1] == "api"
assert "--include" not in sys.argv and "-i" not in sys.argv
os.write(1, b"prefix-\\xce")
os.write(1, b"\\xb1-tail\\n")
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())
    chunks: list[str] = []

    metadata = await client.stream_text(
        "api",
        "repos/octo/repo/actions/jobs/1/logs",
        "-X",
        "GET",
        on_chunk=chunks.append,
    )

    assert "".join(chunks) == "prefix-\u03b1-tail\n"
    assert metadata.attempts == 1


@pytest.mark.asyncio
async def test_stream_text_rejects_write_classification_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "executed"
    _fake_gh(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with pytest.raises(ValueError, match="read-only"):
        await client.stream_text("issue", "create", on_chunk=lambda _: None)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_stream_text_does_not_retry_after_partial_output(
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
    chunks: list[str] = []

    with pytest.raises(RuntimeError, match="connection reset"):
        await client.stream_text(
            "api",
            "repos/octo/repo/actions/jobs/1/logs",
            "-X",
            "GET",
            on_chunk=chunks.append,
        )

    assert "".join(chunks) == "partial"
    assert counter.read_text() == "1"


@pytest.mark.asyncio
async def test_stream_text_timeout_terminates_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(
        tmp_path,
        "import os, time\nos.write(1, b'prefix')\ntime.sleep(30)\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())
    chunks: list[str] = []

    with pytest.raises(RuntimeError, match="timed out"):
        await client.stream_text(
            "api",
            "repos/octo/repo/actions/jobs/1/logs",
            "-X",
            "GET",
            on_chunk=chunks.append,
            timeout=0.05,
        )

    assert "".join(chunks) == "prefix"


@pytest.mark.asyncio
async def test_stream_text_rejects_direct_allow_escape_sequences_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(tmp_path, "print('{}')\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with pytest.raises(ValueError, match="rejects direct --allow-escape-sequences"):
        await client.stream_text(
            "api",
            "--allow-escape-sequences",
            "repos/octo/repo/actions/jobs/1/logs",
            "-X",
            "GET",
            on_chunk=lambda _: None,
        )


@pytest.mark.asyncio
async def test_stream_text_injects_allow_escape_sequences_for_api_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        """import os, sys
from pathlib import Path
Path("/tmp/captured_argv").write_text("\\n".join(sys.argv))
os.write(1, b"ok\\n")
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())
    chunks: list[str] = []

    await client.stream_text(
        "api",
        "repos/octo/repo/actions/jobs/1/logs",
        "-X",
        "GET",
        on_chunk=chunks.append,
        allow_escape_sequences=True,
    )

    argv_lines = (
        Path("/tmp/captured_argv").read_text().splitlines()
        if Path("/tmp/captured_argv").exists()
        else []
    )
    assert any("--allow-escape-sequences" in line for line in argv_lines)


@pytest.mark.asyncio
async def test_stream_text_does_not_inject_escape_flag_for_non_api_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        """import os, sys
from pathlib import Path
Path("/tmp/captured_argv2").write_text("\\n".join(sys.argv))
print("{}")
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())
    chunks: list[str] = []

    await client.stream_text(
        "issue",
        "list",
        on_chunk=chunks.append,
        allow_escape_sequences=True,
    )

    argv_lines = (
        Path("/tmp/captured_argv2").read_text().splitlines()
        if Path("/tmp/captured_argv2").exists()
        else []
    )
    assert all("--allow-escape-sequences" not in line for line in argv_lines)
