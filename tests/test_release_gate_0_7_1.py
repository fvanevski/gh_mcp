"""Final release-integration regression gate for version 0.7.1."""

from __future__ import annotations

import tomllib
from pathlib import Path

from mcp_gh_server import __version__
from mcp_gh_server.server import mcp

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.7.1"
EXPECTED_TOOL_COUNT = 61
EXPECTED_READ_ONLY_COUNT = 40
EXPECTED_WRITE_COUNT = 21
RELEASE_NEW_TOOLS = {
    "gh_get_merge_requirements",
    "gh_compare_commits",
    "gh_list_artifact_files",
    "gh_read_artifact_file",
    "gh_get_api_rate_status",
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

    # This gate is immutable 0.7.1 release authority. Breaking 0.8.x child issues may
    # intentionally make these assertions fail until issue #61 versions and integrates
    # the new public surface; child issues must not rewrite these released counts.
    assert len(tools) == EXPECTED_TOOL_COUNT
    assert len(read_only) == EXPECTED_READ_ONLY_COUNT
    assert len(tools) - len(read_only) == EXPECTED_WRITE_COUNT
    assert tools.keys() >= RELEASE_NEW_TOOLS
    assert FORBIDDEN_PUBLIC_TOOLS.isdisjoint(tools)

    for name in RELEASE_NEW_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        assert annotations.idempotent_hint is True


def test_release_docs_preserve_authority_and_describe_development_transition() -> None:
    readme = (ROOT / "README.md").read_text()
    qwen = (ROOT / "QWEN.md").read_text()
    gate = (ROOT / "docs" / "release_gate_0_7_1.md").read_text()
    documents = (readme, qwen, gate)

    for document in documents:
        assert "0.7.1" in document
        assert "61 public MCP tools" in document
        assert "40 read-only" in document
        assert "21 write" in document
        for tool_name in RELEASE_NEW_TOOLS:
            assert tool_name in document

    # Current development documentation must not pretend the already-retired generic
    # writes remain public merely because the package version has not yet advanced.
    for document in documents:
        assert "59 public MCP tools" in document
        assert "19 write" in document
        assert "#61" in document

    heading = "### Write (current unreleased surface: 19)"
    assert heading in readme
    write_section = readme.split(heading, 1)[1].split("\n## ", 1)[0]
    assert "- `gh_create_release`:" not in write_section
    assert "- `gh_run_workflow`:" not in write_section
    assert "- `gh_create_release_exact`:" in write_section
    assert "- `gh_run_workflow_exact`:" in write_section

    non_goals = (
        "arbitrary public `gh api`",
        "generic shell",
        "administrator",
        "automatic",
        "artifact deletion",
        "branch-protection",
    )
    for phrase in non_goals:
        assert phrase in gate


def test_release_artifact_content_contract_is_documented() -> None:
    artifact_docs = (ROOT / "docs" / "gh_artifact_content.md").read_text()

    for phrase in (
        "exact `artifact_id`",
        "path traversal",
        "symbolic links",
        "strict UTF-8",
        "SHA-256",
        "Temporary ZIP state",
    ):
        assert phrase in artifact_docs
