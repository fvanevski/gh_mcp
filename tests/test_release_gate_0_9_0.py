"""Final release-integration regression gate for version 0.9.0."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from mcp_gh_server import __version__
from mcp_gh_server.current_write_tool_schema import PUBLIC_WRITE_TOOLS, WRITE_TOOL_METADATA
from mcp_gh_server.server import mcp
from mcp_gh_server.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.9.0"
EXPECTED_TOOL_COUNT = 62
EXPECTED_READ_ONLY_COUNT = 41
EXPECTED_WRITE_COUNT = 21
EXPECTED_PYREFLY_REQUIREMENT = "pyrefly==1.1.1"
EXPECTED_WRITE_TOOLS = {
    "gh_commit_files",
    "gh_patch_files",
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
    "gh_request_pr_changes",
    "gh_run_workflow_exact",
    "gh_set_issue_state",
    "gh_set_pr_draft_state",
    "gh_approve_pr",
    "gh_comment_pr_review",
}
RETIRED_PUBLIC_TOOLS = {
    "gh_create_release",
    "gh_run_workflow",
    "gh_submit_pr_review",
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


def test_pyrefly_is_pinned_as_release_static_type_authority() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirements = [
        line.strip()
        for line in (ROOT / "requirements-typecheck.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    pyrefly = project["tool"]["pyrefly"]

    assert requirements == [EXPECTED_PYREFLY_REQUIREMENT]
    assert pyrefly["project-includes"] == ["src/**/*.py"]
    assert pyrefly["project-excludes"] == ["tests/**/*.py"]
    assert pyrefly["search-path"] == ["src", "."]
    assert pyrefly["python-platform"] == "linux"
    assert pyrefly["python-version"] == "3.12"
    assert "baseline" not in pyrefly
    assert not (ROOT / "pyrefly-baseline.json").exists()


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


def test_current_write_registry_is_explicit_not_old_inventory_minus_retired_tool() -> None:
    path = ROOT / "src" / "mcp_gh_server" / "current_write_tool_schema.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    imported_from_legacy_facade: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "write_tool_schema":
            imported_from_legacy_facade.update(alias.name for alias in node.names)

    assert "PUBLIC_WRITE_TOOLS" not in imported_from_legacy_facade
    assert "gh_submit_pr_review" not in imported_from_legacy_facade
    assert "gh_submit_pr_review" not in path.read_text()


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
    gate = (ROOT / "docs" / "release_gate_0_9_0.md").read_text()
    contract = (ROOT / "docs" / "write-schema-contract.md").read_text()
    agents = (ROOT / "AGENTS.md").read_text()
    surface = "Version 0.9.0 exposes 62 public MCP tools: 41 read-only and 21 write."
    contract_surface = "62 total public MCP tools: 41 read-only and 21 write"
    pyrefly_command = "uv run --with-requirements requirements-typecheck.txt pyrefly check"

    assert surface in readme
    assert surface in gate
    assert contract_surface in contract
    assert "exact 62/41/21 tool inventory" in agents
    assert "18 non-review writes" in agents
    assert "21 current public write names" in agents
    assert "gh_patch_files" in contract
    assert "gh_patch_files" in agents

    for document in (readme, gate, agents):
        assert pyrefly_command in document
        assert "uv run mypy" not in document

    for tool_name in EXPECTED_WRITE_TOOLS:
        assert tool_name in gate
    for retired in RETIRED_PUBLIC_TOOLS:
        assert retired in gate
    assert "host interception" in gate.casefold()
    assert "Do not retry" in gate
    assert "tests/test_reviewer_client_isolation.py" in gate
    assert "## Disposable live exercise" in gate
    assert "## Final live evidence" in gate
    assert "NOT SAFELY INDUCED" in gate
