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
