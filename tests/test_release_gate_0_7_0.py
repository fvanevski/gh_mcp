"""Historical release-floor regression gate for version 0.7.0."""

from __future__ import annotations

import tomllib
from pathlib import Path

from mcp_gh_server import __version__
from mcp_gh_server.server import mcp

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_VERSION = (0, 7, 0)
MINIMUM_TOOL_COUNT = 56
MINIMUM_READ_ONLY_COUNT = 35
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


def _version_tuple(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def test_release_versions_preserve_0_7_0_floor_and_current_agreement() -> None:
    """Keep current version authorities aligned without pinning later releases to 0.7.0."""

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    editable_packages = [
        package
        for package in lock["package"]
        if package.get("name") == "mcp-gh-server" and package.get("source") == {"editable": "."}
    ]

    assert len(editable_packages) == 1
    assert project["project"]["version"] == __version__
    assert editable_packages[0]["version"] == __version__
    assert _version_tuple(__version__) >= HISTORICAL_VERSION


async def test_release_tool_inventory_floor_and_non_goals() -> None:
    """Preserve the shipped 0.7.0 surface while allowing additive later development."""

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    read_only = {
        name
        for name, tool in tools.items()
        if tool.annotations is not None and tool.annotations.read_only_hint is True
    }

    # 0.7.0 established a historical release floor, not a permanent ceiling on
    # additive tools developed before later version gates. The exact current
    # inventory remains enforced separately by test_tool_schema_snapshot.py.
    assert len(tools) >= MINIMUM_TOOL_COUNT
    assert len(read_only) >= MINIMUM_READ_ONLY_COUNT
    assert len(tools) - len(read_only) == EXPECTED_WRITE_COUNT
    assert tools.keys() >= RELEASE_NEW_TOOLS
    assert FORBIDDEN_PUBLIC_TOOLS.isdisjoint(tools)


def test_release_document_preserves_historical_surface() -> None:
    """Keep the immutable 0.7.0 release record exact as current docs advance."""

    gate = (ROOT / "docs" / "release_gate_0_7_0.md").read_text()

    assert "Version 0.7.0 exposes 56 public MCP tools: 35 read-only and 21 write." in gate
    for tool_name in RELEASE_NEW_TOOLS:
        assert tool_name in gate
    for phrase in (
        "arbitrary public `gh <args...>`",
        "arbitrary public `gh api`",
        "generic shell or subprocess MCP tool",
        "administrator bypasses",
        "automatic repeated workflow rerun or dispatch",
        "artifact or log deletion",
        "branch-protection or ruleset mutation",
    ):
        assert phrase in gate
