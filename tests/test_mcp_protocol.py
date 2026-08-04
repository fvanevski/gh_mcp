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


def _write_fake_commit_gh(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh"
    state_path = tmp_path / "ref-read-count"
    script = r"""#!/usr/bin/env python3
import json, pathlib, sys

args = sys.argv[1:]
head = "a" * 40
commit = "e" * 40
state = pathlib.Path(__STATE__)

if args[:2] != ["api", "graphql"] and args[:1] != ["api"]:
    raise SystemExit(2)
endpoint = args[1]
if endpoint == "repos/octo/repo/git/ref/heads/feature":
    count = int(state.read_text()) if state.exists() else 0
    state.write_text(str(count + 1))
    print(json.dumps({"object": {"sha": head if count == 0 else commit}}))
elif endpoint == "repos/octo/repo":
    print(json.dumps({"node_id": "R_repo"}))
elif endpoint == f"repos/octo/repo/git/commits/{head}":
    print(json.dumps({"tree": {"sha": "b" * 40}}))
elif endpoint == "repos/octo/repo/git/blobs":
    payload = json.loads(pathlib.Path(args[args.index("--input") + 1]).read_text())
    assert payload == {"content": "complete content\n", "encoding": "utf-8"}
    print(json.dumps({"sha": "c" * 40}))
elif endpoint == "repos/octo/repo/git/trees":
    print(json.dumps({"sha": "d" * 40}))
elif endpoint == "repos/octo/repo/git/commits":
    print(json.dumps({"sha": commit, "html_url": f"https://github.com/octo/repo/commit/{commit}"}))
elif endpoint == "graphql":
    payload = json.loads(pathlib.Path(args[args.index("--input") + 1]).read_text())
    update = payload["variables"]["input"]["refUpdates"][0]
    assert update["beforeOid"] == head
    assert update["afterOid"] == commit
    assert update["force"] is False
    print(json.dumps({"data": {"updateRefs": {"clientMutationId": None}}}))
else:
    raise SystemExit(2)
""".replace("__STATE__", repr(str(state_path)))
    fake_gh.write_text(script)
    fake_gh.chmod(0o700)


@pytest.mark.asyncio
async def test_registered_tool_schemas_and_annotations() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert len(tools) == 34
    assert "gh_get_file_contents" in tools
    assert "gh_commit_files" in tools
    assert "approval" not in tools["gh_create_issue"].input_schema["properties"]
    assert "force" not in tools["gh_create_label"].input_schema["properties"]
    assert tools["gh_upsert_label"].annotations.destructive_hint is True
    assert tools["gh_create_issue"].annotations.destructive_hint is False
    assert tools["gh_run_workflow"].annotations.destructive_hint is True
    assert tools["gh_commit_files"].annotations.destructive_hint is True
    commit_schema = tools["gh_commit_files"].input_schema
    assert commit_schema["properties"]["files"]["items"]["$ref"].endswith("/$defs/CommitFile")
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
        assert len(tools_after_failure.tools) == 34


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
        assert len((await session.list_tools()).tools) == 34


@pytest.mark.asyncio
async def test_stdio_atomic_content_commit_executes_through_mcp(tmp_path: Path) -> None:
    _write_fake_commit_gh(tmp_path)
    project = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "MCP_GH_ALLOW_WRITE_COMMANDS": "true",
            "MCP_GH_ALLOW_CONTENT_COMMITS": "true",
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
            "gh_commit_files",
            {
                "owner": "octo",
                "repo": "repo",
                "branch": "feature",
                "expected_head_sha": "a" * 40,
                "files": [{"path": "docs/file.md", "content": "complete content\n"}],
                "commit_message": "Atomic commit",
            },
            allow_input_required=True,
        )

        assert not isinstance(result, InputRequiredResult)
        assert result.is_error is False
        assert result.structured_content["ref_updated"] is True
        assert result.structured_content["commit_sha"] == "e" * 40


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
            assert len((await session.list_tools()).tools) == 34
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
            assert len((await session.list_tools()).tools) == 34
    finally:
        get_settings.cache_clear()
