"""Non-destructive inventory and optional underlying-gh exercise for gh_mcp tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

EXPECTED_VERSION = "0.7.1"
EXPECTED_TOOL_COUNT = 61
REQUIRED_0_7_1_TOOLS = {
    "gh_get_merge_requirements",
    "gh_compare_commits",
    "gh_list_artifact_files",
    "gh_read_artifact_file",
    "gh_get_api_rate_status",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="verify the exact release inventory without issuing GitHub requests",
    )
    return parser.parse_args()


async def verify_inventory() -> None:
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from mcp_gh_server import __version__
    from mcp_gh_server.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    if __version__ != EXPECTED_VERSION:
        raise RuntimeError(
            f"server version mismatch: expected {EXPECTED_VERSION}, got {__version__}"
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

    print(f"✓ Server version: {__version__}")
    print(f"✓ Exact public tool count: {len(tools)}")
    print("✓ Required 0.7.1 read-only tools are registered")


def live_exercises() -> list[tuple[str, list[str]]]:
    """Return bounded non-destructive gh CLI probes for representative tool routes."""

    return [
        ("gh_info", ["gh", "auth", "status", "--json", "hosts"]),
        (
            "gh_get_api_rate_status",
            ["gh", "api", "rate_limit", "-X", "GET", "--jq", "{rate: .rate}"],
        ),
        (
            "gh_search_repos",
            ["gh", "search", "repos", "--json", "fullName", "--limit", "3", "--", "cli/cli"],
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
            ["gh", "issue", "view", "1", "--repo", "cli/cli", "--json", "title,number,state"],
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
            ["gh", "pr", "view", "1", "--repo", "cli/cli", "--json", "title,number,state"],
        ),
        (
            "gh_get_repo",
            ["gh", "repo", "view", "cli/cli", "--json", "nameWithOwner,name"],
        ),
        (
            "gh_list_repos",
            ["gh", "repo", "list", "cli", "--json", "nameWithOwner", "--limit", "3"],
        ),
        (
            "gh_list_releases",
            ["gh", "release", "list", "--repo", "cli/cli", "--json", "tagName", "--limit", "3"],
        ),
        (
            "gh_get_release",
            ["gh", "release", "view", "v2.97.0", "--repo", "cli/cli", "--json", "tagName,name"],
        ),
        (
            "gh_list_workflows",
            ["gh", "workflow", "list", "--repo", "cli/cli", "--json", "id,name", "--limit", "3"],
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


def describe_json(stdout: str) -> str:
    try:
        data: Any = json.loads(stdout)
    except json.JSONDecodeError:
        return f"{len(stdout)} chars"
    if isinstance(data, list):
        return f"{len(data)} items"
    if isinstance(data, dict):
        return f"{len(data)} keys"
    return "JSON"


def exercise_underlying_gh() -> bool:
    gh_env = {
        **os.environ,
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GH_PAGER": "cat",
        "PAGER": "cat",
    }
    failed = 0
    for tool_name, command in live_exercises():
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
                env=gh_env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"✗ {tool_name}: FAILED - timeout")
            failed += 1
            continue

        if result.returncode != 0:
            stderr = result.stderr.strip() or "no stderr"
            print(f"✗ {tool_name}: FAILED - {stderr[:100]}")
            failed += 1
            continue
        print(f"✓ {tool_name}: OK ({describe_json(result.stdout)})")

    print(f"Representative live probes: {len(live_exercises()) - failed} passed, {failed} failed")
    return failed == 0


def main() -> int:
    args = parse_args()
    print("=" * 60)
    print("gh_mcp MCP Tool Exercise")
    print("=" * 60)
    try:
        asyncio.run(verify_inventory())
    except Exception as exc:
        print(f"✗ Inventory verification failed: {exc}")
        return 1

    if args.inventory_only:
        return 0

    print("\nExercising representative underlying gh CLI read commands:")
    print("-" * 60)
    return 0 if exercise_underlying_gh() else 1


if __name__ == "__main__":
    raise SystemExit(main())
