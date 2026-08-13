"""Regression tests for empty pull-request check results."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.gh_client import GhClient
from mcp_gh_server.request_governor import GitHubRequestError
from mcp_gh_server.server import AppContext, gh_get_pr_checks
from mcp_gh_server.settings import Settings

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
CHANGED_HEAD_SHA = "c" * 40


def _write_fake_gh(
    tmp_path: Path,
    *,
    checks_stderr: str,
    expect_required: bool,
    move_head: bool = False,
) -> None:
    state_path = tmp_path / "pr-read-count"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        f"""#!/usr/bin/env python3
import json
import pathlib
import sys

args = sys.argv[1:]
base_sha = {BASE_SHA!r}
head_sha = {HEAD_SHA!r}
changed_head_sha = {CHANGED_HEAD_SHA!r}
state = pathlib.Path({str(state_path)!r})

if args[:2] == ["api", "repos/octo/repo/pulls/47"]:
    count = int(state.read_text()) if state.exists() else 0
    state.write_text(str(count + 1))
    current_head = changed_head_sha if {move_head!r} and count > 0 else head_sha
    print(json.dumps({{"base": {{"sha": base_sha}}, "head": {{"sha": current_head}}}}))
elif args[:2] == ["pr", "checks"]:
    if ("--required" in args) is not {expect_required!r}:
        print("unexpected --required argument state", file=sys.stderr)
        raise SystemExit(2)
    print({checks_stderr!r}, file=sys.stderr)
    raise SystemExit(1)
else:
    raise SystemExit(2)
"""
    )
    fake_gh.chmod(0o700)


def _context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    checks_stderr: str,
    expect_required: bool,
    move_head: bool = False,
) -> Any:
    _write_fake_gh(
        tmp_path,
        checks_stderr=checks_stderr,
        expect_required=expect_required,
        move_head=move_head,
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    settings = Settings()
    app = AppContext(client=GhClient(settings), settings=settings)
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


async def test_no_checks_returns_empty_structured_result_and_verifies_shas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(
        tmp_path,
        monkeypatch,
        checks_stderr="no checks reported on the 'feature' branch",
        expect_required=False,
    )

    result = await gh_get_pr_checks("octo", "repo", 47, ctx=ctx)

    assert result.base_sha == BASE_SHA
    assert result.head_sha == HEAD_SHA
    assert result.total_count == 0
    assert result.truncated is False
    assert result.checks == []


async def test_no_required_checks_returns_empty_structured_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(
        tmp_path,
        monkeypatch,
        checks_stderr="no required checks reported on the 'feature' branch",
        expect_required=True,
    )

    result = await gh_get_pr_checks("octo", "repo", 47, ctx=ctx, required_only=True)

    assert result.base_sha == BASE_SHA
    assert result.head_sha == HEAD_SHA
    assert result.total_count == 0
    assert result.truncated is False
    assert result.checks == []


async def test_unrelated_gh_client_error_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(
        tmp_path,
        monkeypatch,
        checks_stderr="authentication failed",
        expect_required=False,
    )

    with pytest.raises(GitHubRequestError, match="authentication failed"):
        await gh_get_pr_checks("octo", "repo", 47, ctx=ctx)


async def test_no_required_checks_is_not_suppressed_without_required_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(
        tmp_path,
        monkeypatch,
        checks_stderr="no required checks reported on the 'feature' branch",
        expect_required=False,
    )

    with pytest.raises(GitHubRequestError, match="no required checks reported"):
        await gh_get_pr_checks("octo", "repo", 47, ctx=ctx)


async def test_empty_checks_still_rejects_pr_sha_movement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(
        tmp_path,
        monkeypatch,
        checks_stderr="no checks reported on the 'feature' branch",
        expect_required=False,
        move_head=True,
    )

    with pytest.raises(RuntimeError, match="base or head changed during the read"):
        await gh_get_pr_checks("octo", "repo", 47, ctx=ctx)
