"""Subprocess-boundary tests for the asynchronous gh client."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pytest

from mcp_gh_server.gh_client import GhClient
from mcp_gh_server.settings import Settings


def _fake_gh(tmp_path: Path, source: str) -> Path:
    executable = tmp_path / "gh"
    executable.write_text(f"#!/usr/bin/env python3\n{source}")
    executable.chmod(0o700)
    return executable


@pytest.mark.asyncio
async def test_run_disables_prompts_detaches_stdin_and_propagates_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        """import json, os, sys
print(json.dumps({
    "prompt": os.getenv("GH_PROMPT_DISABLED"),
    "git_prompt": os.getenv("GIT_TERMINAL_PROMPT"),
    "pager": os.getenv("GH_PAGER"),
    "token": os.getenv("GH_TOKEN"),
    "stdin": sys.stdin.read(),
}))
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings(github_token="secret-token"))

    detached = await client.run("inspect")
    supplied = await client.run("inspect", stdin_text="body text")

    assert detached["stdin"] == ""
    assert supplied["stdin"] == "body text"
    assert detached["prompt"] == "1"
    assert detached["git_prompt"] == "0"
    assert detached["pager"] == "cat"
    assert detached["token"] == "secret-token"


@pytest.mark.asyncio
async def test_run_times_out_without_hanging_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        "import sys, time\nif sys.argv[1] == 'slow': time.sleep(30)\nelse: print('{}')\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with pytest.raises(RuntimeError, match="timed out"):
        await client.run("slow", timeout=0.05)
    assert await client.run("fast")


@pytest.mark.asyncio
async def test_run_terminates_child_when_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        "import sys, time\nif sys.argv[1] == 'slow': time.sleep(30)\nelse: print('{}')\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())
    task = asyncio.create_task(client.run("slow"))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await client.run("fast")


@pytest.mark.asyncio
async def test_debug_log_redacts_user_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _fake_gh(tmp_path, "print('{}')\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with caplog.at_level(logging.DEBUG, logger="mcp_gh_server.gh_client"):
        await client.run("issue", "create", "--title", "private title")

    assert "private title" not in caplog.text
    assert "<redacted>" in caplog.text


@pytest.mark.asyncio
async def test_run_preserves_bounded_github_validation_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        """import json, sys
print(json.dumps({
    "message": "Validation Failed",
    "errors": [{
        "resource": "PullRequestReview",
        "code": "custom",
        "message": "Review cannot be requested from pull request author.",
        "value": "must-not-be-exposed",
    }],
    "documentation_url": "https://docs.github.com/rest/pulls/reviews",
    "status": "422",
}))
print("gh: Unprocessable Entity (HTTP 422)", file=sys.stderr)
raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with pytest.raises(RuntimeError) as raised:
        await client.run("api", "repos/octo/repo/pulls/224/reviews")

    message = str(raised.value)
    assert "Unprocessable Entity (HTTP 422)" in message
    assert "Validation Failed" in message
    assert "Review cannot be requested from pull request author" in message
    assert "documentation_url" in message
    assert "must-not-be-exposed" not in message


@pytest.mark.asyncio
async def test_run_accepts_documented_status_exit_when_json_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        "import json\nprint(json.dumps([{'name': 'CI', 'bucket': 'fail'}]))\nraise SystemExit(1)\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    result = await client.run("pr", "checks", expected_returncode={0, 1, 8})

    assert result[0]["bucket"] == "fail"


@pytest.mark.asyncio
async def test_run_rejects_documented_status_exit_without_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        "import sys\nprint('authentication failed', file=sys.stderr)\nraise SystemExit(1)\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with pytest.raises(RuntimeError, match="without structured output"):
        await client.run("pr", "checks", expected_returncode={0, 1, 8})


@pytest.mark.asyncio
async def test_stream_text_does_not_receive_escape_flag_for_ordinary_api_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        """import os, sys
from pathlib import Path
Path("/tmp/captured_argv_no_opt").write_text("\\n".join(sys.argv))
print("{}")
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
    )

    argv_lines = (
        Path("/tmp/captured_argv_no_opt").read_text().splitlines()
        if Path("/tmp/captured_argv_no_opt").exists()
        else []
    )
    assert all("--allow-escape-sequences" not in line for line in argv_lines)


@pytest.mark.asyncio
async def test_stream_text_injects_allow_escape_sequences_for_opted_in_api_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        """import os, sys
from pathlib import Path
Path("/tmp/captured_argv_opt").write_text("\\n".join(sys.argv))
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
        Path("/tmp/captured_argv_opt").read_text().splitlines()
        if Path("/tmp/captured_argv_opt").exists()
        else []
    )
    assert any("--allow-escape-sequences" in line for line in argv_lines)


@pytest.mark.asyncio
async def test_stream_text_rejects_direct_allow_escape_sequences_bare_flag(
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
async def test_stream_text_rejects_direct_allow_escape_sequences_with_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(tmp_path, "print('{}')\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with pytest.raises(ValueError, match="rejects direct --allow-escape-sequences"):
        await client.stream_text(
            "api",
            "--allow-escape-sequences=true",
            "repos/octo/repo/actions/jobs/1/logs",
            "-X",
            "GET",
            on_chunk=lambda _: None,
        )


@pytest.mark.asyncio
async def test_stream_text_rejects_non_api_use_of_allow_escape_sequences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(tmp_path, "print('{}')\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = GhClient(Settings())

    with pytest.raises(ValueError, match="only on gh api reads"):
        await client.stream_text(
            "issue",
            "list",
            on_chunk=lambda _: None,
            allow_escape_sequences=True,
        )
