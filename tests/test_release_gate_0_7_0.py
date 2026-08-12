"""Release-integration regression gate for version 0.7.0."""

from __future__ import annotations

import tomllib
from pathlib import Path

from mcp_gh_server import __version__
from mcp_gh_server.server import mcp

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.7.0"
EXPECTED_TOOL_COUNT = 56
EXPECTED_READ_ONLY_COUNT = 35
EXPECTED_WRITE_COUNT = 21
RELEASE_NEW_TOOLS = {
    "gh_get_ref",
    "gh_get_commit",
    "gh_list_run_artifacts",
    "gh_get_artifact",
    "gh_get_job_logs",
    "gh_get_run_logs",
    "gh_run_workflow_exact",
    "gh_create_release_exact",
    "gh_set_issue_state",
    "gh_list_pr_reviews",
    "gh_get_pr_review_state",
    "gh_set_pr_draft_state",
}
FORBIDDEN_PUBLIC_TOOLS = {
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


def test_release_versions_and_lockfile_agree() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    editable_packages = [
        package
        for package in lock["package"]
        if package.get("name") == "mcp-gh-server" and package.get("source") == {"editable": "."}
    ]

    assert __version__ == EXPECTED_VERSION
    assert project["project"]["version"] == EXPECTED_VERSION
    assert len(editable_packages) == 1
    assert editable_packages[0]["version"] == EXPECTED_VERSION


async def test_release_tool_inventory_and_non_goals() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    read_only = {
        name
        for name, tool in tools.items()
        if tool.annotations is not None and tool.annotations.read_only_hint is True
    }

    assert len(tools) == EXPECTED_TOOL_COUNT
    assert len(read_only) == EXPECTED_READ_ONLY_COUNT
    assert len(tools) - len(read_only) == EXPECTED_WRITE_COUNT
    assert tools.keys() >= RELEASE_NEW_TOOLS
    assert FORBIDDEN_PUBLIC_TOOLS.isdisjoint(tools)


def test_release_docs_report_current_surface() -> None:
    readme = (ROOT / "README.md").read_text()
    qwen = (ROOT / "QWEN.md").read_text()
    gate = (ROOT / "docs" / "release_gate_0_7_0.md").read_text()

    assert "0.7.0" in readme
    assert "Read-only (35)" in readme
    assert "Write (21)" in readme
    for document in (qwen, gate):
        assert "0.7.0" in document
        assert "56 public MCP tools" in document
        assert "35 read-only" in document
        assert "21 write" in document
