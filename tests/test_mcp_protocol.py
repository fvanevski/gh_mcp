"""Protocol-facing schema and session-liveness regression tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import InputRequiredResult

from mcp_gh_server.server import mcp
from mcp_gh_server.settings import get_settings


def _write_fake_gh(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args[:2] == ["issue", "create"]:
    assert sys.stdin.read() == ""
    print("https://github.com/octo/repo/issues/42")
elif args[:2] == ["issue", "view"]:
    print(json.dumps({"number": 42, "title": "Created", "url": args[2]}))
else:
    raise SystemExit(2)
"""
    )
    fake_gh.chmod(0o700)


@pytest.mark.asyncio
async def test_registered_tool_schemas_and_annotations() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert len(tools) == 32
    assert "approval" not in tools["gh_create_issue"].input_schema["properties"]
    assert "force" not in tools["gh_create_label"].input_schema["properties"]
    assert tools["gh_upsert_label"].annotations.destructive_hint is True
    assert tools["gh_create_issue"].annotations.destructive_hint is False
    assert tools["gh_run_workflow"].annotations.destructive_hint is True
    assert all(tool.annotations.open_world_hint is True for tool in tools.values())


@pytest.mark.asyncio
async def test_stdio_write_denial_does_not_elicit_or_lock_session() -> None:
    project = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(
        {
            "MCP_GH_ALLOW_WRITE_COMMANDS": "false",
            "MCP_GH_TRANSPORT": "stdio",
            "MCP_GH_ENV_FILE": str(project / ".env.example"),
        }
    )
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_gh_server"],
        cwd=project,
        env=env,
    )

    async with (
        stdio_client(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "gh_create_issue",
            {"owner": "octo", "repo": "repo", "title": "must not be created"},
            allow_input_required=True,
        )
        assert not isinstance(result, InputRequiredResult)
        assert result.is_error is True
        tools_after_failure = await session.list_tools()
        assert len(tools_after_failure.tools) == 32


@pytest.mark.asyncio
async def test_stdio_write_executes_once_without_elicitation_using_fake_gh(tmp_path: Path) -> None:
    _write_fake_gh(tmp_path)
    project = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "MCP_GH_ALLOW_WRITE_COMMANDS": "true",
            "MCP_GH_ALLOWED_REPOSITORIES": "octo/repo",
            "MCP_GH_TRANSPORT": "stdio",
            "MCP_GH_ENV_FILE": str(project / ".env.example"),
        }
    )
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_gh_server"],
        cwd=project,
        env=env,
    )

    async with (
        stdio_client(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "gh_create_issue",
            {"owner": "octo", "repo": "repo", "title": "Created"},
            allow_input_required=True,
        )
        assert not isinstance(result, InputRequiredResult)
        assert result.is_error is False
        assert len((await session.list_tools()).tools) == 32


@pytest.mark.asyncio
async def test_streamable_http_write_denial_keeps_session_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_GH_ALLOW_WRITE_COMMANDS", "false")
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
            result = await session.call_tool(
                "gh_create_issue",
                {"owner": "octo", "repo": "repo", "title": "must not be created"},
                allow_input_required=True,
            )
            assert not isinstance(result, InputRequiredResult)
            assert result.is_error is True
            assert len((await session.list_tools()).tools) == 32
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_streamable_http_write_executes_without_nested_input_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fake_gh(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("MCP_GH_ALLOW_WRITE_COMMANDS", "true")
    monkeypatch.setenv("MCP_GH_ALLOWED_REPOSITORIES", "octo/repo")
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
            result = await session.call_tool(
                "gh_create_issue",
                {"owner": "octo", "repo": "repo", "title": "Created"},
                allow_input_required=True,
            )
            assert not isinstance(result, InputRequiredResult)
            assert result.is_error is False
            assert len((await session.list_tools()).tools) == 32
    finally:
        get_settings.cache_clear()
