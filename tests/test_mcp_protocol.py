"""Protocol-level tests for MCP 2.0 tool registration and resources."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from mcp_gh_server.asgi import app
from mcp_gh_server.models import ServerInfo
from mcp_gh_server.server import AppContext, gh_server_info, mcp
from mcp_gh_server.settings import Settings


@dataclass
class FakeClient:
    results: list[Any] = field(default_factory=list)
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if not self.results:
            raise RuntimeError("unexpected call")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(self, *args: str, **kwargs: Any) -> Any:
        return await self.run(*args, **kwargs)

    def clamp_max_results(self, requested: int | None) -> int:
        return requested if requested is not None else 30


def _context(client: FakeClient, **settings_overrides: Any) -> Any:
    settings = Settings(**settings_overrides)
    app_context = AppContext(client=client, settings=settings)  # type: ignore[arg-type]
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_context))


def _tool_names(tools: list[Any]) -> list[str]:
    return sorted(tool.name for tool in tools)


def _tool_by_name(tools: list[Any], name: str) -> Any:
    return next(tool for tool in tools if tool.name == name)


def _assert_read_only(tool: Any) -> None:
    annotations = tool.annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True


def _assert_additive_write(tool: Any) -> None:
    annotations = tool.annotations
    assert annotations is not None
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is False


def _assert_destructive_write(tool: Any) -> None:
    annotations = tool.annotations
    assert annotations is not None
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is True
    assert annotations.idempotent_hint is False


async def test_tool_inventory_and_annotations() -> None:
    tools = await mcp.list_tools()
    names = _tool_names(tools)

    assert len(names) == 58
    assert len(names) == len(set(names))

    for name in (
        "gh_server_info",
        "gh_info",
        "gh_get_api_rate_status",
        "gh_search_repos",
        "gh_search_issues",
        "gh_search_code",
        "gh_list_issues",
        "gh_get_issue",
        "gh_list_labels",
        "gh_list_milestones",
        "gh_list_prs",
        "gh_get_pr",
        "gh_get_pr_diff",
        "gh_list_pr_files",
        "gh_list_pr_commits",
        "gh_get_pr_checks",
        "gh_list_pr_reviews",
        "gh_get_pr_review_state",
        "gh_get_merge_requirements",
        "gh_get_repo",
        "gh_list_repos",
        "gh_get_file_contents",
        "gh_get_ref",
        "gh_get_commit",
        "gh_compare_commits",
        "gh_list_releases",
        "gh_get_release",
        "gh_list_workflows",
        "gh_get_workflow",
        "gh_list_runs",
        "gh_get_run",
        "gh_list_run_jobs",
        "gh_get_failed_run_logs",
        "gh_get_job_logs",
        "gh_get_run_logs",
        "gh_list_run_artifacts",
        "gh_get_artifact",
        "gh_list_artifact_files",
        "gh_read_artifact_file",
        "gh_watch_run",
    ):
        _assert_read_only(_tool_by_name(tools, name))

    for name in (
        "gh_create_issue",
        "gh_create_label",
        "gh_create_milestone",
        "gh_create_comment",
        "gh_create_pr",
        "gh_submit_pr_review",
        "gh_create_repo",
        "gh_create_release_exact",
        "gh_create_branch",
        "gh_create_branch_from_sha",
    ):
        _assert_additive_write(_tool_by_name(tools, name))

    for name in (
        "gh_edit_issue",
        "gh_set_issue_state",
        "gh_edit_label",
        "gh_edit_pr",
        "gh_set_pr_draft_state",
        "gh_merge_pr",
        "gh_commit_files",
        "gh_run_workflow_exact",
    ):
        _assert_destructive_write(_tool_by_name(tools, name))

    for retired in ("gh_run_workflow", "gh_create_release", "gh_upsert_label"):
        assert retired not in names


async def test_tool_schemas_are_bounded_and_exact() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    merge = tools["gh_merge_pr"]
    merge_props = merge.input_schema["properties"]
    assert merge_props["expected_head_sha"]["pattern"] == "^[0-9A-Fa-f]{40}$"
    assert merge_props["method"]["enum"] == ["merge", "squash", "rebase"]

    dispatch = tools["gh_run_workflow_exact"]
    dispatch_props = dispatch.input_schema["properties"]
    assert dispatch_props["workflow_id"]["minimum"] == 1
    assert dispatch_props["expected_workflow_path"]["pattern"] == (
        r"^\.github/workflows/[^/\x00-\x1f\x7f]+\.ya?ml$"
    )
    assert dispatch_props["ref"]["pattern"] == r"^(?:heads|tags)/.+$"
    assert dispatch_props["expected_ref_sha"]["pattern"] == "^[0-9A-Fa-f]{40}$"

    commit = tools["gh_commit_files"]
    commit_props = commit.input_schema["properties"]
    assert commit_props["expected_head_sha"]["pattern"] == "^[0-9A-Fa-f]{40}$"
    files_schema = commit_props["files"]
    assert files_schema["minItems"] == 1
    assert files_schema["maxItems"] == 1000


async def test_tool_return_schemas_are_structured() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name in (
        "gh_create_issue",
        "gh_edit_issue",
        "gh_set_issue_state",
        "gh_create_label",
        "gh_edit_label",
        "gh_create_milestone",
        "gh_create_comment",
        "gh_create_pr",
        "gh_edit_pr",
        "gh_set_pr_draft_state",
        "gh_submit_pr_review",
        "gh_merge_pr",
        "gh_create_repo",
        "gh_commit_files",
        "gh_create_release_exact",
        "gh_run_workflow_exact",
        "gh_create_branch",
        "gh_create_branch_from_sha",
    ):
        schema = tools[name].output_schema
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        assert "properties" in schema


async def test_no_generic_executor_or_admin_surface() -> None:
    names = _tool_names(await mcp.list_tools())
    forbidden = {
        "gh_exec",
        "gh_api",
        "gh_shell",
        "gh_run_command",
        "gh_rerun_workflow",
        "gh_delete_artifact",
        "gh_delete_run_logs",
        "gh_set_branch_protection",
        "gh_set_ruleset",
    }
    assert forbidden.isdisjoint(names)


async def test_server_info_structured_content_is_local_and_current() -> None:
    client = FakeClient()
    result = await gh_server_info(
        ctx=_context(
            client,
            allow_write_commands=True,
            allow_content_commits=True,
            allow_pr_merge=True,
            allow_repo_creation=True,
            allow_release_creation=True,
            allow_workflow_dispatch=True,
        )
    )

    assert isinstance(result, ServerInfo)
    assert result.server_name == "mcp-gh-server"
    assert result.server_version == "0.8.0"
    assert result.tool_schema_version == "0.8.0"
    assert result.tool_count == 58
    assert result.write_commands_enabled is True
    assert result.content_commits_enabled is True
    assert result.pr_merge_enabled is True
    assert result.repo_creation_enabled is True
    assert result.release_creation_enabled is True
    assert result.workflow_dispatch_enabled is True
    assert client.calls == []


async def test_server_info_defaults_keep_all_write_gates_off() -> None:
    client = FakeClient()
    result = await gh_server_info(ctx=_context(client))

    assert result.write_commands_enabled is False
    assert result.content_commits_enabled is False
    assert result.pr_merge_enabled is False
    assert result.repo_creation_enabled is False
    assert result.release_creation_enabled is False
    assert result.workflow_dispatch_enabled is False
    assert client.calls == []


def _stdio_server_parameters() -> StdioServerParameters:
    root = Path(__file__).resolve().parents[1]
    return StdioServerParameters(
        command="uv",
        args=["run", "--directory", str(root), "--frozen", "mcp-gh"],
        env={"MCP_GH_ENV_FILE": str(root / ".env.example")},
    )


@asynccontextmanager
async def _initialized_stdio_session() -> Any:
    async with (
        stdio_client(_stdio_server_parameters()) as read_stream_write_stream,
        ClientSession(read_stream_write_stream[0], read_stream_write_stream[1]) as session,
    ):
        await session.initialize()
        yield session


async def test_stdio_protocol_lists_current_surface() -> None:
    async with _initialized_stdio_session() as session:
        tools = await session.list_tools()

    names = sorted(tool.name for tool in tools.tools)
    assert len(names) == 58
    assert "gh_run_workflow_exact" in names
    assert "gh_create_release_exact" in names
    assert "gh_run_workflow" not in names
    assert "gh_create_release" not in names
    assert "gh_upsert_label" not in names


async def test_stdio_server_info_reports_current_version() -> None:
    async with _initialized_stdio_session() as session:
        result = await session.call_tool("gh_server_info", arguments={})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["server_version"] == "0.8.0"
    assert result.structured_content["tool_schema_version"] == "0.8.0"
    assert result.structured_content["tool_count"] == 58


async def test_stdio_disabled_write_fails_without_disabling_namespace() -> None:
    async with _initialized_stdio_session() as session:
        result = await session.call_tool(
            "gh_create_issue",
            arguments={"owner": "octo", "repo": "repo", "title": "blocked"},
        )
        assert result.is_error is True
        tools_after = await session.list_tools()

    assert len(tools_after.tools) == 58
    assert "gh_create_issue" in {tool.name for tool in tools_after.tools}


async def test_stdio_invalid_schema_is_rejected_before_execution() -> None:
    async with _initialized_stdio_session() as session:
        result = await session.call_tool(
            "gh_merge_pr",
            arguments={
                "owner": "octo",
                "repo": "repo",
                "number": 1,
                "expected_head_sha": "not-a-sha",
                "method": "merge",
            },
        )
        assert result.is_error is True


@pytest.fixture
async def streamable_server() -> Any:
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)

    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()

    config.port = port
    task = __import__("asyncio").create_task(server.serve())
    try:
        import asyncio

        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("streamable HTTP test server did not start")
        yield f"http://{host}:{port}/mcp"
    finally:
        server.should_exit = True
        await task


async def test_streamable_http_protocol_initializes_and_lists_tools(streamable_server: str) -> None:
    async with streamable_http_client(streamable_server) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()

    assert initialized.server_info.name == "GitHub CLI"
    assert initialized.server_info.version == "0.8.0"
    assert len(tools.tools) == 58


async def test_streamable_http_server_info_is_current(streamable_server: str) -> None:
    async with streamable_http_client(streamable_server) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool("gh_server_info", arguments={})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["server_version"] == "0.8.0"
    assert result.structured_content["tool_schema_version"] == "0.8.0"
    assert result.structured_content["tool_count"] == 58


async def test_streamable_http_write_denial_does_not_poison_server(streamable_server: str) -> None:
    async with streamable_http_client(streamable_server) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                "gh_create_issue",
                arguments={"owner": "octo", "repo": "repo", "title": "blocked"},
            )
            assert result.is_error is True
            server_info_after_denial = await session.call_tool("gh_server_info", arguments={})

    assert server_info_after_denial.is_error is False
    assert server_info_after_denial.structured_content is not None
    assert server_info_after_denial.structured_content["server_version"] == "0.8.0"


async def test_streamable_http_schema_failure_does_not_invoke_tool(streamable_server: str) -> None:
    with patch("mcp_gh_server.tools.pr_writes.gh_merge_pr") as mocked:
        async with streamable_http_client(streamable_server) as streams:
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "gh_merge_pr",
                    arguments={
                        "owner": "octo",
                        "repo": "repo",
                        "number": 1,
                        "expected_head_sha": "bad",
                        "method": "merge",
                    },
                )
                assert result.is_error is True

    mocked.assert_not_called()


async def test_tool_metadata_has_no_hidden_confirmation_fields() -> None:
    tools = await mcp.list_tools()
    for tool in tools:
        if tool.name.startswith("gh_") and tool.annotations is not None:
            properties = tool.input_schema.get("properties", {})
            for forbidden in (
                "confirm",
                "confirmation",
                "authorized",
                "approval",
                "safety_justification",
            ):
                assert forbidden not in properties


async def test_call_tool_structured_content_round_trips_write_outcome() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    schema = tools["gh_create_comment"].output_schema
    assert schema is not None
    for field_name in (
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "request_id",
        "warning",
    ):
        assert field_name in schema["properties"]


async def test_text_content_is_not_used_as_write_result_contract() -> None:
    tools = await mcp.list_tools()
    for tool in tools:
        if tool.name in {
            "gh_create_issue",
            "gh_edit_issue",
            "gh_set_issue_state",
            "gh_create_label",
            "gh_edit_label",
            "gh_create_milestone",
            "gh_create_comment",
            "gh_create_pr",
            "gh_edit_pr",
            "gh_set_pr_draft_state",
            "gh_submit_pr_review",
            "gh_merge_pr",
            "gh_create_repo",
            "gh_commit_files",
            "gh_create_release_exact",
            "gh_run_workflow_exact",
            "gh_create_branch",
            "gh_create_branch_from_sha",
        }:
            assert tool.output_schema is not None


async def test_no_tool_description_claims_admin_bypass() -> None:
    for tool in await mcp.list_tools():
        description = (tool.description or "").casefold()
        assert "administrator bypass" not in description
        assert "admin bypass" not in description


async def test_error_result_does_not_disable_tool_namespace() -> None:
    async with _initialized_stdio_session() as session:
        result = await session.call_tool(
            "gh_create_issue",
            arguments={"owner": "octo", "repo": "repo", "title": "blocked"},
        )
        assert result.is_error is True
        tools_after = await session.list_tools()

    assert len(tools_after.tools) == 58
    assert "gh_create_issue" in {tool.name for tool in tools_after.tools}


def test_streamable_http_app_is_mountable() -> None:
    assert callable(app)


def test_text_content_import_remains_sdk_compatible() -> None:
    assert TextContent is not None
