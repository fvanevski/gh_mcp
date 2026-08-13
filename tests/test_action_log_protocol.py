"""Protocol-level regression coverage for streamed bounded Actions log tools."""

from __future__ import annotations

import os
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_gh_server.server import mcp
from mcp_gh_server.settings import get_settings


def _write_fake_action_logs_gh(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        r"""#!/usr/bin/env python3
import json, sys

args = sys.argv[1:]
head_sha = "a" * 40

if args[:2] == ["run", "view"] or any(
    "/actions/runs/123/logs" in arg or "/actions/runs/123/attempts/2/logs" in arg
    for arg in args
):
    print("forbidden unbounded run-log route", file=sys.stderr)
    raise SystemExit(91)

if args[:2] == ["api", "repos/octo/repo/actions/jobs/456"]:
    print(json.dumps({
        "id": 456,
        "run_id": 123,
        "head_sha": head_sha,
        "name": "tests",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/octo/repo/actions/runs/123/job/456",
    }))
elif args[:2] == ["api", "repos/octo/repo/actions/runs/123/attempts/2"]:
    print(json.dumps({
        "id": 123,
        "run_attempt": 2,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/octo/repo/actions/runs/123",
    }))
elif args[:2] == ["api", "repos/octo/repo/actions/runs/123/attempts/2/jobs"]:
    assert "-X" in args and args[args.index("-X") + 1] == "GET"
    print(json.dumps({
        "total_count": 1,
        "jobs": [{
            "id": 456,
            "run_id": 123,
            "head_sha": head_sha,
            "name": "tests",
            "status": "completed",
            "conclusion": "success",
        }],
    }))
elif any("repos/octo/repo/actions/jobs/456/logs" in arg for arg in args):
    assert "--include" not in args and "-i" not in args
    assert "--paginate" not in args and "--slurp" not in args
    sys.stdout.write("job completed successfully")
else:
    print(f"unexpected fake gh args: {args!r}", file=sys.stderr)
    raise SystemExit(2)
"""
    )
    fake_gh.chmod(0o700)


@pytest.mark.asyncio
async def test_streamable_http_returns_run_and_job_log_evidence_without_run_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_fake_action_logs_gh(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("MCP_GH_ALLOW_WRITE_COMMANDS", "false")
    monkeypatch.setenv("MCP_GH_TRANSPORT", "streamable-http")
    get_settings.cache_clear()
    base_url = "http://127.0.0.1:8766"
    http_app = mcp.streamable_http_app(stateless_http=True)
    transport = httpx2.ASGITransport(app=http_app)

    try:
        async with (
            http_app.router.lifespan_context(http_app),
            httpx2.AsyncClient(transport=transport, base_url=base_url) as http_client,
            streamable_http_client(f"{base_url}/mcp", http_client=http_client) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()

            run_result = await session.call_tool(
                "gh_get_run_logs",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "run_id": 123,
                    "attempt": 2,
                    "tail_bytes": 12,
                },
            )
            assert run_result.is_error is False
            assert run_result.structured_content["run_id"] == 123
            assert run_result.structured_content["attempt"] == 2
            assert run_result.structured_content["head_sha"] == "a" * 40
            assert run_result.structured_content["text"] == "successfully"
            assert run_result.structured_content["truncated"] is True

            job_result = await session.call_tool(
                "gh_get_job_logs",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "job_id": 456,
                    "attempt": 2,
                    "start_marker": "completed",
                },
            )
            assert job_result.is_error is False
            assert job_result.structured_content["run_id"] == 123
            assert job_result.structured_content["job_id"] == 456
            assert job_result.structured_content["attempt"] == 2
            assert job_result.structured_content["text"] == "completed successfully"
            assert job_result.structured_content["truncated"] is True
    finally:
        get_settings.cache_clear()
