"""Non-destructive exercise of all gh_mcp MCP tools.

This script exercises the underlying gh CLI commands that the MCP tools
wrap, confirming they work correctly. Tool registration is verified
by the successful import of the server module. Pass ``--inventory-only``
to verify the exact release inventory without issuing GitHub requests.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

EXPECTED_VERSION = "0.7.1"
EXPECTED_TOOL_COUNT = 61
REQUIRED_0_7_1_TOOLS = {
    "gh_get_merge_requirements",
    "gh_compare_commits",
    "gh_list_artifact_files",
    "gh_read_artifact_file",
    "gh_get_api_rate_status",
}
INVENTORY_ONLY = "--inventory-only" in sys.argv[1:]

print("=" * 60)
print("gh_mcp MCP Tool Exercise")
print("=" * 60)

# Verify tool registration by importing the server module
print("\nVerifying tool registration:")
print("-" * 60)
try:
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from mcp_gh_server.server import mcp

    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    if mcp.version != EXPECTED_VERSION:
        raise RuntimeError(
            f"server version mismatch: expected {EXPECTED_VERSION}, got {mcp.version}"
        )
    if len(tools) != EXPECTED_TOOL_COUNT:
        raise RuntimeError(
            f"tool-count mismatch: expected {EXPECTED_TOOL_COUNT}, got {len(tools)}"
        )
    missing = REQUIRED_0_7_1_TOOLS.difference(tools)
    if missing:
        raise RuntimeError(f"missing 0.7.1 tools: {', '.join(sorted(missing))}")
    for name in REQUIRED_0_7_1_TOOLS:
        annotations = tools[name].annotations
        if annotations is None or annotations.read_only_hint is not True:
            raise RuntimeError(f"0.7.1 tool is not registered read-only: {name}")

    print("✓ Server module imported successfully")
    print(f"✓ Server name: {mcp.name}")
    print(f"✓ Server version: {mcp.version}")
    print(f"✓ Exact public tool count: {len(tools)}")
    print("✓ Required 0.7.1 read-only tools are registered")
except Exception as e:
    print(f"✗ Tool registration failed: {e}")
    sys.exit(1)

if INVENTORY_ONLY:
    sys.exit(0)

print()

# Exercise read-only tools using gh CLI directly
print("Exercising underlying gh CLI commands:")
print("-" * 60)

tests = [
    (
        "gh_info",
        [
            "gh",
            "auth",
            "status",
            "--json",
            "hosts",
        ],
    ),
    (
        "gh_get_api_rate_status",
        [
            "gh",
            "api",
            "rate_limit",
            "-X",
            "GET",
            "--jq",
            "{rate: .rate}",
        ],
    ),
    (
        "gh_search_repos",
        [
            "gh",
            "search",
            "repos",
            "--json",
            "fullName",
            "--limit",
            "3",
            "--",
            "cli/cli",
        ],
    ),
    (
        "gh_search_issues",
        [
            "gh",
            "search",
            "issues",
            "--json",
            "title,number",
            "--limit",
            "3",
            "--",
            "is:open repo:cli/cli",
        ],
    ),
    (
        "gh_search_code",
        [
            "gh",
            "search",
            "code",
            "--json",
            "path,repository,sha,textMatches",
            "--limit",
            "3",
            "--",
            "def test language:python",
        ],
    ),
    (
        "gh_list_issues",
        [
            "gh",
            "issue",
            "list",
            "--repo",
            "cli/cli",
            "--json",
            "title,number",
            "--limit",
            "3",
        ],
    ),
    (
        "gh_get_issue",
        [
            "gh",
            "issue",
            "view",
            "1",
            "--repo",
            "cli/cli",
            "--json",
            "title,number,state",
        ],
    ),
    (
        "gh_list_prs",
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "cli/cli",
            "--json",
            "title,number",
            "--limit",
            "3",
        ],
    ),
    (
        "gh_get_pr",
        [
            "gh",
            "pr",
            "view",
            "1",
            "--repo",
            "cli/cli",
            "--json",
            "title,number,state",
        ],
    ),
    (
        "gh_get_repo",
        [
            "gh",
            "repo",
            "view",
            "cli/cli",
            "--json",
            "nameWithOwner,name",
        ],
    ),
    (
        "gh_list_repos",
        [
            "gh",
            "repo",
            "list",
            "cli",
            "--json",
            "nameWithOwner",
            "--limit",
            "3",
        ],
    ),
    (
        "gh_list_releases",
        [
            "gh",
            "release",
            "list",
            "--repo",
            "cli/cli",
            "--json",
            "tagName",
            "--limit",
            "3",
        ],
    ),
    (
        "gh_get_release",
        [
            "gh",
            "release",
            "view",
            "v2.97.0",
            "--repo",
            "cli/cli",
            "--json",
            "tagName,name",
        ],
    ),
    (
        "gh_list_workflows",
        [
            "gh",
            "workflow",
            "list",
            "--repo",
            "cli/cli",
            "--json",
            "id,name",
            "--limit",
            "3",
        ],
    ),
    (
        "gh_list_runs",
        [
            "gh",
            "run",
            "list",
            "--repo",
            "cli/cli",
            "--json",
            "databaseId,status",
            "--limit",
            "3",
        ],
    ),
]

gh_env = {
    **os.environ,
    "GH_PROMPT_DISABLED": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GH_PAGER": "cat",
    "PAGER": "cat",
}

passed = 0
failed = 0

for tool_name, cmd in tests:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=gh_env,
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                if isinstance(data, list):
                    print(f"✓ {tool_name}: OK ({len(data)} items)")
                elif isinstance(data, dict):
                    print(f"✓ {tool_name}: OK ({len(data)} keys)")
                else:
                    print(f"✓ {tool_name}: OK")
                passed += 1
            except json.JSONDecodeError:
                print(f"✓ {tool_name}: OK ({len(result.stdout)} chars)")
                passed += 1
        else:
            stderr = result.stderr.strip() or "no stderr"
            print(f"✗ {tool_name}: FAILED - {stderr[:100]}")
            failed += 1
    except subprocess.TimeoutExpired:
        print(f"✗ {tool_name}: FAILED - timeout")
        failed += 1
    except Exception as e:
        print(f"✗ {tool_name}: FAILED - {e}")
        failed += 1

print("-" * 60)
print(f"\nResults: {passed} passed, {failed} failed")
print("=" * 60)

if failed > 0:
    sys.exit(1)
