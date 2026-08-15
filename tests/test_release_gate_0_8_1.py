"""Final release-integration regression gate for version 0.8.1."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from mcp_gh_server import __version__
from mcp_gh_server.server import mcp
from mcp_gh_server.settings import Settings
from mcp_gh_server.write_tool_schema import PUBLIC_WRITE_TOOLS, WRITE_TOOL_METADATA

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.8.1"
EXPECTED_TOOL_COUNT = 58
EXPECTED_READ_ONLY_COUNT = 40
EXPECTED_WRITE_COUNT = 18
EXPECTED_WRITE_TOOLS = {
    "gh_commit_files",
    "gh_create_branch",
    "gh_create_branch_from_sha",
    "gh_create_comment",
    "gh_create_issue",
    "gh_create_label",
    "gh_create_milestone",
    "gh_create_pr",
    "gh_create_release_exact",
    "gh_create_repo",
    "gh_edit_issue",
    "gh_edit_label",
    "gh_edit_pr",
    "gh_merge_pr",
    "gh_run_workflow_exact",
    "gh_set_issue_state",
    "gh_set_pr_draft_state",
    "gh_submit_pr_review",
}
RETIRED_PUBLIC_TOOLS = {
    "gh_create_release",
    "gh_run_workflow",
    "gh_upsert_label",
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


async def test_release_tool_inventory_is_exact() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    read_only = {
        name
        for name, tool in tools.items()
        if tool.annotations is not None and tool.annotations.read_only_hint is True
    }
    write_tools = set(tools) - read_only

    assert len(tools) == EXPECTED_TOOL_COUNT
    assert len(read_only) == EXPECTED_READ_ONLY_COUNT
    assert len(write_tools) == EXPECTED_WRITE_COUNT
    assert write_tools == EXPECTED_WRITE_TOOLS
    assert RETIRED_PUBLIC_TOOLS.isdisjoint(tools)
    assert FORBIDDEN_PUBLIC_TOOLS.isdisjoint(tools)


def _registered_tool_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    registered: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            function = decorator.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "tool"
                and isinstance(function.value, ast.Name)
                and function.value.id == "mcp"
            ):
                registered.add(node.name)
    return registered


def _legacy_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "legacy_" in node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names if "legacy_" in alias.name)
    return imports


def test_public_writes_have_one_canonical_registration_path() -> None:
    facade_names = [tool.__name__ for tool in PUBLIC_WRITE_TOOLS]
    assert len(facade_names) == EXPECTED_WRITE_COUNT
    assert len(set(facade_names)) == EXPECTED_WRITE_COUNT
    assert set(facade_names) == EXPECTED_WRITE_TOOLS
    assert set(WRITE_TOOL_METADATA) == EXPECTED_WRITE_TOOLS

    server = (ROOT / "src" / "mcp_gh_server" / "server.py").read_text()
    assert "mcp.remove_tool(" not in server
    assert server.count("mcp.add_tool(") == 1

    directly_registered: set[str] = set()
    tools_dir = ROOT / "src" / "mcp_gh_server" / "tools"
    for path in tools_dir.glob("*.py"):
        directly_registered |= _registered_tool_function_names(path)

    assert EXPECTED_WRITE_TOOLS.isdisjoint(directly_registered)


def test_obsolete_write_compatibility_paths_and_imports_are_absent() -> None:
    package = ROOT / "src" / "mcp_gh_server"
    legacy_paths = sorted(path.name for path in package.glob("legacy_*write*.py"))
    assert legacy_paths == []
    assert not (package / "legacy_write_support.py").exists()
    assert not (package / "legacy_assignee_support.py").exists()

    contracts = (package / "write_contracts.py").read_text()
    assert "LegacyWriteStatus" not in contracts
    assert "legacy_write_status" not in contracts

    stale_imports: dict[str, list[str]] = {}
    for path in ROOT.rglob("*.py"):
        imports = _legacy_imports(path)
        if imports:
            stale_imports[str(path.relative_to(ROOT))] = imports
    assert stale_imports == {}


def test_high_risk_write_gates_remain_default_off() -> None:
    fields = Settings.model_fields
    assert fields["allow_write_commands"].default is False
    assert fields["allow_repo_creation"].default is False
    assert fields["allow_release_creation"].default is False
    assert fields["allow_workflow_dispatch"].default is False
    assert fields["allow_content_commits"].default is False
    assert fields["allow_pr_merge"].default is False
    assert fields["allowed_repo_creation_targets"].default == ""
    assert fields["allowed_workflow_dispatch_targets"].default == ""


def test_release_documentation_matches_runtime_authority() -> None:
    readme = (ROOT / "README.md").read_text()
    gate = (ROOT / "docs" / "release_gate_0_8_1.md").read_text()
    surface = "Version 0.8.1 exposes 58 public MCP tools: 40 read-only and 18 write."

    assert surface in readme
    assert surface in gate
    for tool_name in EXPECTED_WRITE_TOOLS:
        assert tool_name in gate
    for retired in RETIRED_PUBLIC_TOOLS:
        assert retired in gate
    assert "host interception" in gate.casefold()
    assert "Do not retry" in gate
