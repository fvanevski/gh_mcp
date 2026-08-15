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


def _write_fake_exact_branch_gh(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        r"""#!/usr/bin/env python3
import json, pathlib, sys

args = sys.argv[1:]
base_sha = "a" * 40
if args[:2] == ["api", f"repos/octo/repo/git/commits/{base_sha}"]:
    assert args[-2:] == ["-X", "GET"]
    assert sys.stdin.read() == ""
    print(json.dumps({"sha": base_sha}))
elif args[:2] == ["api", "repos/octo/repo/git/refs"]:
    assert args[args.index("-X") + 1] == "POST"
    payload = json.loads(pathlib.Path(args[args.index("--input") + 1]).read_text())
    assert payload == {"ref": "refs/heads/feature/exact", "sha": base_sha}
    assert sys.stdin.read() == ""
    print(json.dumps({"ref": payload["ref"], "object": {"sha": base_sha}}))
else:
    raise SystemExit(2)
"""
    )
    fake_gh.chmod(0o700)


def _write_fake_routing_gh(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        r"""#!/usr/bin/env python3
import json, sys

args = sys.argv[1:]
blob_sha = "b" * 40
base_sha = "c" * 40
head_sha = "d" * 40
if args[:2] == ["api", "repos/octo/repo/contents/scripts/drain_index_jobs.py"]:
    assert args[-4:] == ["-X", "GET", "-f", "ref=" + "a" * 40]
    print(json.dumps({"type": "file", "sha": blob_sha}))
elif args[:2] == ["api", f"repos/octo/repo/git/blobs/{blob_sha}"]:
    print(json.dumps({"encoding": "base64", "content": "cHJpbnQoJ29rJykK"}))
elif args[:2] == ["api", "repos/octo/repo/pulls/224"]:
    assert args[-2:] == ["-X", "GET"]
    print(json.dumps({
        "number": 224, "title": "Review PR", "state": "open",
        "html_url": "https://github.com/octo/repo/pull/224",
        "user": {"login": "author"}, "labels": [{"name": "review"}],
        "comments": 1, "review_comments": 2,
        "base": {"ref": "main", "sha": base_sha},
        "head": {"ref": "feature", "sha": head_sha},
        "draft": False, "additions": 1, "deletions": 0, "changed_files": 1
    }))
elif args[:2] == ["api", f"repos/octo/repo/compare/{base_sha}...{head_sha}"]:
    if "Accept: application/vnd.github.v3.diff" in args:
        print("diff --git a/file.txt b/file.txt\n+reviewed")
    else:
        assert args[args.index("-X") + 1] == "GET"
        assert "page=1" in args and "per_page=30" in args and "--jq" in args
        print(json.dumps({
            "status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
            "base_commit": {"sha": base_sha},
            "merge_base_commit": {"sha": base_sha},
            "commits": [{
                "sha": head_sha,
                "html_url": "https://github.com/octo/repo/commit/" + head_sha,
                "commit": {
                    "message": "Compared commit",
                    "author": {"name": "Author", "date": "2026-08-12T10:00:00Z"},
                    "committer": {"name": "Committer", "date": "2026-08-12T10:01:00Z"}
                },
                "author": {"login": "author"}, "committer": {"login": "committer"}
            }],
            "files": [{
                "filename": "file.txt", "status": "modified", "additions": 1,
                "deletions": 0, "changes": 1, "sha": "e" * 40,
                "previous_filename": None,
                "blob_url": "https://github.com/octo/repo/blob/" + head_sha + "/file.txt",
                "raw_url": "https://github.com/octo/repo/raw/" + head_sha + "/file.txt",
                "contents_url": "https://api.github.com/repos/octo/repo/contents/file.txt"
            }]
        }))
elif args[:2] == ["api", "repos/octo/repo/pulls/224/files"]:
    assert "-X" in args and args[args.index("-X") + 1] == "GET"
    print(json.dumps([{
        "filename": "file.txt", "status": "modified", "additions": 1,
        "deletions": 0, "changes": 1, "sha": "e" * 40
    }]))
elif args[:2] == ["api", "repos/octo/repo/pulls/224/commits"]:
    assert "-X" in args and args[args.index("-X") + 1] == "GET"
    print(json.dumps([{
        "sha": head_sha, "html_url": "https://github.com/octo/repo/commit/" + head_sha,
        "commit": {"message": "Reviewed commit", "author": {}, "committer": {}}
    }]))
elif args[:2] == ["api", "repos/octo/repo/actions/runs/123/attempts/1/jobs"]:
    assert "-X" in args and args[args.index("-X") + 1] == "GET"
    print(json.dumps({
        "total_count": 1,
        "jobs": [{
            "id": 456, "name": "tests", "status": "completed",
            "conclusion": "failure", "steps": [{
                "number": 1, "name": "pytest", "status": "completed",
                "conclusion": "failure"
            }]
        }]
    }))
elif args[:2] == ["pr", "checks"]:
    assert "--watch" not in args and "--web" not in args
    print(json.dumps([{
        "name": "tests", "state": "FAILURE", "bucket": "fail",
        "workflow": "CI", "description": "Tests failed",
        "link": "https://github.com/octo/repo/actions/runs/123"
    }]))
    raise SystemExit(1)
elif args[:2] == ["run", "view"]:
    if "--log-failed" in args:
        print("tests\tpytest\tassertion failed")
    else:
        print(json.dumps({
            "attempt": 1, "headSha": head_sha, "status": "completed",
            "conclusion": "failure",
            "url": "https://github.com/octo/repo/actions/runs/123"
        }))
elif args[:2] == ["workflow", "list"]:
    print(json.dumps([{
        "id": 7,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "state": "active"
    }]))
else:
    raise SystemExit(2)
"""
    )
    fake_gh.chmod(0o700)


def _write_fake_pr_completion_gh(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        r"""#!/usr/bin/env python3
import json, pathlib, sys

args = sys.argv[1:]
base_sha = "a" * 40
head_sha = "b" * 40
merge_sha = "c" * 40
review_url = "https://github.com/octo/repo/pull/224#pullrequestreview-91"

if args[:2] == ["api", "repos/octo/repo/pulls/224"]:
    print(json.dumps({
        "base": {"sha": base_sha}, "head": {"sha": head_sha},
        "user": {"login": "author"}
    }))
elif args[:2] == ["api", "user"]:
    print(json.dumps({"login": "reviewer"}))
elif args[:2] == ["api", "repos/octo/repo/pulls/224/reviews"]:
    payload = json.loads(pathlib.Path(args[args.index("--input") + 1]).read_text())
    assert payload == {
        "body": "Reviewed exact revision.",
        "event": "APPROVE",
        "commit_id": head_sha,
    }
    print(json.dumps({"id": 91, "state": "APPROVED", "html_url": review_url}))
elif args[:2] == ["api", "repos/octo/repo/pulls/224/reviews/91"]:
    print(json.dumps({
        "id": 91, "state": "APPROVED", "body": "Reviewed exact revision.",
        "html_url": review_url, "commit_id": head_sha,
        "submitted_at": "2026-08-04T18:00:00Z", "user": {"login": "reviewer"}
    }))
elif args[:2] == ["pr", "merge"]:
    assert sys.stdin.read() == "Merge exact reviewed revision."
    assert "--match-head-commit" in args
    assert args[args.index("--match-head-commit") + 1] == head_sha
    assert "--squash" in args
    assert "--admin" not in args and "--delete-branch" not in args
elif args[:2] == ["pr", "view"]:
    print(json.dumps({
        "number": 224, "url": "https://github.com/octo/repo/pull/224",
        "state": "MERGED", "mergedAt": "2026-08-04T18:01:00Z",
        "mergeCommit": {"oid": merge_sha}, "headRefOid": head_sha,
        "mergeStateStatus": "CLEAN", "autoMergeRequest": None
    }))
else:
    raise SystemExit(2)
"""
    )
    fake_gh.chmod(0o700)


@pytest.mark.asyncio
async def test_registered_tool_schemas_and_annotations() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert len(tools) == 58
    assert "gh_run_workflow" not in tools
    assert "gh_create_release" not in tools
    assert "gh_server_info" in tools
    assert "gh_get_api_rate_status" in tools
    assert "gh_get_file_contents" in tools
    assert "gh_get_ref" in tools
    assert "gh_compare_commits" in tools
    assert "gh_commit_files" in tools
    assert "gh_create_branch_from_sha" in tools
    assert "gh_get_pr_diff" in tools
    assert "gh_list_pr_files" in tools
    assert "gh_list_pr_commits" in tools
    assert "gh_submit_pr_review" in tools
    assert "gh_merge_pr" in tools
    assert "gh_set_issue_state" in tools
    assert "gh_set_pr_draft_state" in tools
    assert "gh_get_pr_checks" in tools
    assert "gh_get_merge_requirements" in tools
    assert "gh_list_run_jobs" in tools
    assert "gh_get_failed_run_logs" in tools
    assert "gh_get_job_logs" in tools
    assert "gh_get_run_logs" in tools
    assert "gh_run_workflow_exact" in tools
    assert "gh_list_artifact_files" in tools
    assert "gh_read_artifact_file" in tools
    assert "approval" not in tools["gh_create_issue"].input_schema["properties"]
    assert "force" not in tools["gh_create_label"].input_schema["properties"]
    assert "gh_upsert_label" not in tools
    assert tools["gh_create_issue"].annotations.destructive_hint is False
    assert tools["gh_create_branch_from_sha"].annotations.destructive_hint is False
    assert tools["gh_create_branch_from_sha"].annotations.read_only_hint is False
    assert tools["gh_run_workflow_exact"].annotations.destructive_hint is True
    assert tools["gh_commit_files"].annotations.destructive_hint is True
    assert tools["gh_submit_pr_review"].annotations.read_only_hint is False
    assert tools["gh_submit_pr_review"].annotations.destructive_hint is False
    assert tools["gh_merge_pr"].annotations.destructive_hint is True
    assert tools["gh_set_issue_state"].annotations.read_only_hint is False
    assert tools["gh_set_issue_state"].annotations.destructive_hint is True
    assert tools["gh_set_pr_draft_state"].annotations.read_only_hint is False
    assert tools["gh_set_pr_draft_state"].annotations.destructive_hint is True
    assert tools["gh_get_api_rate_status"].annotations.read_only_hint is True
    assert tools["gh_get_api_rate_status"].annotations.destructive_hint is False
    assert tools["gh_get_api_rate_status"].annotations.idempotent_hint is True
    assert tools["gh_get_api_rate_status"].annotations.open_world_hint is True
    assert tools["gh_get_ref"].description.startswith("Read-only:")
    assert tools["gh_get_ref"].annotations.read_only_hint is True
    assert tools["gh_get_ref"].annotations.destructive_hint is False
    assert tools["gh_get_ref"].annotations.idempotent_hint is True
    assert tools["gh_get_ref"].annotations.open_world_hint is True
    assert tools["gh_compare_commits"].description.startswith("Read-only:")
    assert tools["gh_compare_commits"].annotations.read_only_hint is True
    assert tools["gh_compare_commits"].annotations.destructive_hint is False
    assert tools["gh_compare_commits"].annotations.idempotent_hint is True
    assert tools["gh_compare_commits"].annotations.open_world_hint is True
    for name in (
        "gh_get_pr_checks",
        "gh_get_merge_requirements",
        "gh_list_run_jobs",
        "gh_get_failed_run_logs",
        "gh_get_job_logs",
        "gh_get_run_logs",
        "gh_list_artifact_files",
        "gh_read_artifact_file",
    ):
        assert tools[name].title
        assert tools[name].description.startswith("Read-only:")
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.destructive_hint is False
        assert tools[name].annotations.idempotent_hint is True
        assert tools[name].annotations.open_world_hint is True
    ref_schema = tools["gh_get_ref"].input_schema["properties"]
    assert ref_schema["ref"]["pattern"] == r"^(?:heads|tags)/.+$"
    assert ref_schema["ref"]["maxLength"] == 1024
    ref_output = tools["gh_get_ref"].output_schema["properties"]
    assert ref_output["ref"]["pattern"] == r"^refs/(?:heads|tags)/.+$"
    assert ref_output["found"]["type"] == "boolean"
    compare_schema = tools["gh_compare_commits"].input_schema["properties"]
    assert compare_schema["base_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    assert compare_schema["head_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    assert compare_schema["max_commits"]["anyOf"][0]["minimum"] == 1
    assert compare_schema["max_files"]["anyOf"][0]["minimum"] == 1
    compare_output = tools["gh_compare_commits"].output_schema["properties"]
    assert compare_output["base_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert compare_output["head_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert compare_output["truncated"]["type"] == "boolean"
    assert compare_output["evidence_complete"]["type"] == "boolean"
    assert compare_output["sha256"]["pattern"] == r"^[0-9a-f]{64}$"
    exact_workflow_schema = tools["gh_run_workflow_exact"].input_schema["properties"]
    assert exact_workflow_schema["workflow_id"]["minimum"] == 1
    assert exact_workflow_schema["expected_workflow_path"]["pattern"]
    assert exact_workflow_schema["ref"]["pattern"] == r"^(?:heads|tags)/.+$"
    assert exact_workflow_schema["expected_ref_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    exact_inputs = exact_workflow_schema["inputs"]["anyOf"][0]
    assert exact_inputs["type"] == "object"
    assert exact_inputs["maxProperties"] == 25
    issue_state_schema = tools["gh_set_issue_state"].input_schema["properties"]
    assert issue_state_schema["number"]["minimum"] == 1
    assert issue_state_schema["expected_state"]["enum"] == ["open", "closed"]
    assert issue_state_schema["new_state"]["enum"] == ["open", "closed"]
    assert issue_state_schema["state_reason"]["enum"] == [
        "completed",
        "not_planned",
        "duplicate",
        "reopened",
    ]
    draft_state_schema = tools["gh_set_pr_draft_state"].input_schema["properties"]
    assert draft_state_schema["number"]["minimum"] == 1
    assert draft_state_schema["expected_head_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    assert draft_state_schema["expected_is_draft"]["type"] == "boolean"
    assert draft_state_schema["new_is_draft"]["type"] == "boolean"
    checks_schema = tools["gh_get_pr_checks"].input_schema["properties"]
    assert checks_schema["number"]["minimum"] == 1
    assert checks_schema["max_checks"]["anyOf"][0]["maximum"] == 1_000
    merge_requirements_schema = tools["gh_get_merge_requirements"].input_schema["properties"]
    assert merge_requirements_schema["number"]["minimum"] == 1
    assert merge_requirements_schema["expected_head_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    merge_requirements_output = tools["gh_get_merge_requirements"].output_schema["properties"]
    assert merge_requirements_output["head_matches_expected"]["type"] == "boolean"
    assert merge_requirements_output["exact_head_evidence"]["type"] == "boolean"
    jobs_schema = tools["gh_list_run_jobs"].input_schema["properties"]
    assert jobs_schema["run_id"]["minimum"] == 1
    assert jobs_schema["per_page"]["anyOf"][0]["maximum"] == 100
    logs_schema = tools["gh_get_failed_run_logs"].input_schema["properties"]
    assert logs_schema["max_bytes"]["anyOf"][0]["maximum"] == 1_000_000
    logs_output = tools["gh_get_failed_run_logs"].output_schema["properties"]
    assert logs_output["content"]["type"] == "string"
    assert logs_output["truncated"]["type"] == "boolean"
    assert logs_output["sha256"]["pattern"]
    for name in ("gh_get_job_logs", "gh_get_run_logs"):
        evidence_schema = tools[name].input_schema["properties"]
        assert evidence_schema["attempt"]["minimum"] == 1
        assert evidence_schema["max_bytes"]["anyOf"][0]["maximum"] == 1_000_000
        assert evidence_schema["tail_bytes"]["anyOf"][0]["maximum"] == 1_000_000
        evidence_output = tools[name].output_schema["properties"]
        assert evidence_output["text"]["type"] == "string"
        assert evidence_output["truncated"]["type"] == "boolean"
        assert evidence_output["sha256"]["pattern"]
    artifact_files_schema = tools["gh_list_artifact_files"].input_schema["properties"]
    assert artifact_files_schema["artifact_id"]["minimum"] == 1
    assert artifact_files_schema["per_page"]["anyOf"][0]["maximum"] == 100
    artifact_read_schema = tools["gh_read_artifact_file"].input_schema["properties"]
    assert artifact_read_schema["artifact_id"]["minimum"] == 1
    assert artifact_read_schema["path"]["maxLength"] == 4096
    assert artifact_read_schema["max_bytes"]["anyOf"][0]["maximum"] == 1_000_000
    review_schema = tools["gh_submit_pr_review"].input_schema["properties"]
    assert review_schema["expected_head_sha"]["pattern"]
    assert review_schema["action"]["enum"] == ["approve", "request_changes", "comment"]
    assert review_schema["body"]["maxLength"] == 65_536
    merge_schema = tools["gh_merge_pr"].input_schema["properties"]
    assert merge_schema["expected_head_sha"]["pattern"]
    assert merge_schema["method"]["enum"] == ["merge", "squash", "rebase"]
    assert "admin" not in merge_schema
    assert "delete_branch" not in merge_schema
    assert tools["gh_server_info"].title == "Get MCP server version"
    assert tools["gh_server_info"].input_schema["properties"] == {}
    assert tools["gh_server_info"].annotations.read_only_hint is True
    assert tools["gh_server_info"].annotations.idempotent_hint is True
    assert tools["gh_server_info"].annotations.open_world_hint is False
    assert tools["gh_get_api_rate_status"].title == "Get GitHub API rate status"
    assert tools["gh_get_api_rate_status"].input_schema["properties"] == {}
    assert tools["gh_get_file_contents"].title == "Read repository file"
    assert tools["gh_get_file_contents"].description.startswith("Read-only:")
    assert tools["gh_get_file_contents"].input_schema["properties"]["ref"]["maxLength"] == 1024
    assert tools["gh_get_pr_diff"].annotations.read_only_hint is True
    assert tools["gh_get_pr_diff"].annotations.destructive_hint is False
    assert tools["gh_get_pr"].title == "Get pull request snapshot"
    assert tools["gh_get_pr"].description.startswith("Read-only:")
    assert tools["gh_get_pr"].annotations.read_only_hint is True
    assert tools["gh_get_pr"].annotations.destructive_hint is False
    assert tools["gh_get_pr"].annotations.idempotent_hint is True
    pr_input = tools["gh_get_pr"].input_schema["properties"]
    assert pr_input["owner"]["pattern"]
    assert pr_input["repo"]["pattern"]
    assert pr_input["number"]["minimum"] == 1
    pr_output = tools["gh_get_pr"].output_schema["properties"]
    assert pr_output["labels"]["items"] == {"type": "string"}
    assert pr_output["comments"]["type"] == "integer"
    assert pr_output["comments"]["minimum"] == 0
    assert pr_output["headRefOid"]["type"] == "string"
    assert pr_output["headRefOid"]["pattern"]
    assert pr_output["baseRefOid"]["type"] == "string"
    assert pr_output["baseRefOid"]["pattern"]
    assert (
        tools["gh_get_pr_diff"].input_schema["properties"]["max_bytes"]["anyOf"][0]["maximum"]
        == 1_000_000
    )
    assert (
        tools["gh_list_pr_files"].input_schema["properties"]["per_page"]["anyOf"][0]["maximum"]
        == 100
    )
    assert tools["gh_commit_files"].title == "Commit repository files atomically"
    commit_schema = tools["gh_commit_files"].input_schema
    assert commit_schema["properties"]["expected_head_sha"]["pattern"]
    assert commit_schema["properties"]["files"]["minItems"] == 1
    assert commit_schema["properties"]["files"]["items"]["$ref"].endswith("/$defs/PublicCommitFile")
    issue_branch_schema = tools["gh_create_branch"].input_schema["properties"]
    assert issue_branch_schema["issue_number"]["minimum"] == 1
    assert "branch-name base" in issue_branch_schema["base"]["description"]
    exact_branch_schema = tools["gh_create_branch_from_sha"].input_schema["properties"]
    assert exact_branch_schema["base_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    exact_branch_output = tools["gh_create_branch_from_sha"].output_schema["properties"]
    assert exact_branch_output["base_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert exact_branch_output["created"]["type"] == "boolean"
    assert all(
        tool.annotations.open_world_hint is True
        for name, tool in tools.items()
        if name != "gh_server_info"
    )


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
        assert len(tools_after_failure.tools) == 58


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
        assert len((await session.list_tools()).tools) == 58


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
            assert len((await session.list_tools()).tools) == 58
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
            assert len((await session.list_tools()).tools) == 58
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_streamable_http_exact_sha_branch_keeps_session_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_fake_exact_branch_gh(tmp_path)
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
                "gh_create_branch_from_sha",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "name": "feature/exact",
                    "base_sha": "a" * 40,
                },
                allow_input_required=True,
            )

            assert not isinstance(result, InputRequiredResult)
            assert result.is_error is False
            assert result.structured_content["created"] is True
            assert result.structured_content["base_sha"] == "a" * 40
            assert result.structured_content["ref"] == "refs/heads/feature/exact"

            server_info = await session.call_tool("gh_server_info", {})
            assert server_info.is_error is False
            assert server_info.structured_content["server_version"] == "0.8.1"
            assert len((await session.list_tools()).tools) == 58
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_streamable_http_content_route_keeps_namespace_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exercise the ChatGPT-facing transport and its post-call routing sequence."""

    _write_fake_routing_gh(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("MCP_GH_ALLOW_WRITE_COMMANDS", "false")
    monkeypatch.setenv("MCP_GH_ALLOW_CONTENT_COMMITS", "false")
    monkeypatch.setenv("MCP_GH_ALLOW_PR_MERGE", "false")
    monkeypatch.setenv("MCP_GH_ALLOW_REPO_CREATION", "false")
    monkeypatch.setenv("MCP_GH_ALLOW_RELEASE_CREATION", "false")
    monkeypatch.setenv("MCP_GH_ALLOW_WORKFLOW_DISPATCH", "false")
    monkeypatch.setenv("MCP_GH_TRANSPORT", "streamable-http")
    get_settings.cache_clear()
    caplog.set_level("INFO", logger="mcp_gh_server.server")
    base_url = "http://127.0.0.1:8766"
    http_app = mcp.streamable_http_app(stateless_http=True)
    transport = httpx2.ASGITransport(app=http_app)
    file_arguments = {
        "owner": "octo",
        "repo": "repo",
        "path": "scripts/drain_index_jobs.py",
        "ref": "a" * 40,
    }

    try:
        async with (
            http_app.router.lifespan_context(http_app),
            httpx2.AsyncClient(transport=transport, base_url=base_url) as http_client,
            streamable_http_client(f"{base_url}/mcp", http_client=http_client) as streams,
            ClientSession(*streams) as session,
        ):
            initialized = await session.initialize()
            assert initialized.server_info.version == "0.8.1"

            server_info = await session.call_tool("gh_server_info", {})
            assert server_info.is_error is False
            assert server_info.structured_content == {
                "server_name": "mcp-gh-server",
                "server_version": "0.8.1",
                "tool_schema_version": "0.8.1",
                "transport": "streamable-http",
                "tool_count": 58,
                "write_commands_enabled": False,
                "content_commits_enabled": False,
                "pr_merge_enabled": False,
                "repo_creation_enabled": False,
                "release_creation_enabled": False,
                "workflow_dispatch_enabled": False,
            }

            file_result = await session.call_tool("gh_get_file_contents", file_arguments)
            assert file_result.is_error is False
            assert file_result.structured_content["content"] == "print('ok')\n"

            pr_result = await session.call_tool(
                "gh_get_pr",
                {"owner": "octo", "repo": "repo", "number": 224},
            )
            assert pr_result.is_error is False
            assert pr_result.structured_content["headRefOid"] == "d" * 40
            assert pr_result.structured_content["baseRefOid"] == "c" * 40
            assert pr_result.structured_content["labels"] == ["review"]
            assert pr_result.structured_content["comments"] == 3

            diff_result = await session.call_tool(
                "gh_get_pr_diff",
                {"owner": "octo", "repo": "repo", "number": 224},
            )
            assert diff_result.is_error is False
            assert diff_result.structured_content["base_sha"] == "c" * 40
            assert diff_result.structured_content["head_sha"] == "d" * 40
            assert diff_result.structured_content["truncated"] is False

            compare_result = await session.call_tool(
                "gh_compare_commits",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "base_sha": "c" * 40,
                    "head_sha": "d" * 40,
                },
            )
            assert compare_result.is_error is False
            assert compare_result.structured_content["base_sha"] == "c" * 40
            assert compare_result.structured_content["head_sha"] == "d" * 40
            assert compare_result.structured_content["merge_base_sha"] == "c" * 40
            assert compare_result.structured_content["status"] == "ahead"
            assert compare_result.structured_content["ahead_by"] == 1
            assert compare_result.structured_content["behind_by"] == 0
            assert compare_result.structured_content["evidence_complete"] is True
            assert compare_result.structured_content["truncated"] is False
            assert len(compare_result.structured_content["sha256"]) == 64

            files_result = await session.call_tool(
                "gh_list_pr_files",
                {"owner": "octo", "repo": "repo", "number": 224},
            )
            assert files_result.is_error is False
            assert files_result.structured_content["files"][0]["filename"] == "file.txt"

            commits_result = await session.call_tool(
                "gh_list_pr_commits",
                {"owner": "octo", "repo": "repo", "number": 224},
            )
            assert commits_result.is_error is False
            assert commits_result.structured_content["commits"][0]["message"] == "Reviewed commit"

            checks_result = await session.call_tool(
                "gh_get_pr_checks",
                {"owner": "octo", "repo": "repo", "number": 224},
            )
            assert checks_result.is_error is False
            assert checks_result.structured_content["checks"][0]["bucket"] == "fail"
            assert checks_result.structured_content["head_sha"] == "d" * 40

            jobs_result = await session.call_tool(
                "gh_list_run_jobs",
                {"owner": "octo", "repo": "repo", "run_id": 123, "attempt": 1},
            )
            assert jobs_result.is_error is False
            assert jobs_result.structured_content["jobs"][0]["steps"][0]["name"] == "pytest"

            logs_result = await session.call_tool(
                "gh_get_failed_run_logs",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "run_id": 123,
                    "attempt": 1,
                    "max_bytes": 1_000,
                },
            )
            assert logs_result.is_error is False
            assert "assertion failed" in logs_result.structured_content["content"]
            assert logs_result.structured_content["truncated"] is False

            tools_after_file = await session.list_tools()
            assert {tool.name for tool in tools_after_file.tools} >= {
                "gh_get_api_rate_status",
                "gh_get_file_contents",
                "gh_get_ref",
                "gh_compare_commits",
                "gh_commit_files",
                "gh_create_branch_from_sha",
                "gh_list_workflows",
                "gh_run_workflow_exact",
                "gh_submit_pr_review",
                "gh_merge_pr",
                "gh_set_issue_state",
                "gh_set_pr_draft_state",
                "gh_server_info",
                "gh_get_pr_checks",
                "gh_get_merge_requirements",
                "gh_list_run_jobs",
                "gh_get_failed_run_logs",
                "gh_get_job_logs",
                "gh_get_run_logs",
                "gh_list_artifact_files",
                "gh_read_artifact_file",
            }

            read_result = await session.call_tool(
                "gh_list_workflows",
                {"owner": "octo", "repo": "repo"},
            )
            assert read_result.is_error is False
            assert read_result.structured_content["total_count"] == 1

            denied_write = await session.call_tool(
                "gh_commit_files",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "branch": "feature",
                    "expected_head_sha": "a" * 40,
                    "files": [{"path": "file.txt", "content": "replacement\n"}],
                    "commit_message": "Denied by server policy",
                },
                allow_input_required=True,
            )
            assert not isinstance(denied_write, InputRequiredResult)
            assert denied_write.is_error is True
            assert "MCP_GH_ALLOW_WRITE_COMMANDS" in denied_write.content[0].text

            denied_exact_branch = await session.call_tool(
                "gh_create_branch_from_sha",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "name": "feature/exact",
                    "base_sha": "a" * 40,
                },
                allow_input_required=True,
            )
            assert not isinstance(denied_exact_branch, InputRequiredResult)
            assert denied_exact_branch.is_error is True
            assert "MCP_GH_ALLOW_WRITE_COMMANDS" in denied_exact_branch.content[0].text

            denied_review = await session.call_tool(
                "gh_submit_pr_review",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "number": 224,
                    "expected_head_sha": "d" * 40,
                    "action": "approve",
                },
                allow_input_required=True,
            )
            assert not isinstance(denied_review, InputRequiredResult)
            assert denied_review.is_error is True
            assert "MCP_GH_ALLOW_WRITE_COMMANDS" in denied_review.content[0].text

            denied_merge = await session.call_tool(
                "gh_merge_pr",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "number": 224,
                    "expected_head_sha": "d" * 40,
                    "method": "squash",
                },
                allow_input_required=True,
            )
            assert not isinstance(denied_merge, InputRequiredResult)
            assert denied_merge.is_error is True
            assert "MCP_GH_ALLOW_WRITE_COMMANDS" in denied_merge.content[0].text

            denied_issue_state = await session.call_tool(
                "gh_set_issue_state",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "number": 18,
                    "expected_state": "open",
                    "new_state": "closed",
                    "state_reason": "completed",
                },
                allow_input_required=True,
            )
            assert not isinstance(denied_issue_state, InputRequiredResult)
            assert denied_issue_state.is_error is True
            assert "MCP_GH_ALLOW_WRITE_COMMANDS" in denied_issue_state.content[0].text

            server_info_after_denial = await session.call_tool("gh_server_info", {})
            assert server_info_after_denial.is_error is False
            assert server_info_after_denial.structured_content["server_version"] == "0.8.1"

            second_file_result = await session.call_tool("gh_get_file_contents", file_arguments)
            assert second_file_result.is_error is False
            assert len((await session.list_tools()).tools) == 58
    finally:
        get_settings.cache_clear()

    messages = [record.getMessage() for record in caplog.records]
    assert sum("tool=gh_get_file_contents" in message for message in messages) == 2
    assert sum("tool=gh_get_pr_diff" in message for message in messages) == 1
    assert sum(message.endswith("tool=gh_get_pr") for message in messages) == 1
    assert sum(message.endswith("tool=gh_get_pr_checks") for message in messages) == 1
    assert sum(message.endswith("tool=gh_list_run_jobs") for message in messages) == 1
    assert sum(message.endswith("tool=gh_get_failed_run_logs") for message in messages) == 1
    assert sum("tool=gh_list_pr_files" in message for message in messages) == 1
    assert sum("tool=gh_list_pr_commits" in message for message in messages) == 1
    assert sum("tool=gh_commit_files" in message for message in messages) == 1
    assert sum("tool=gh_create_branch_from_sha" in message for message in messages) == 1
    assert sum("tool=gh_submit_pr_review" in message for message in messages) == 1
    assert sum("tool=gh_merge_pr" in message for message in messages) == 1
    assert sum("tool=gh_server_info" in message for message in messages) == 2


@pytest.mark.asyncio
async def test_streamable_http_formal_review_then_merge_without_nested_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_fake_pr_completion_gh(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("MCP_GH_ALLOW_WRITE_COMMANDS", "true")
    monkeypatch.setenv("MCP_GH_ALLOW_PR_MERGE", "true")
    monkeypatch.setenv("MCP_GH_ALLOW_REPO_CREATION", "false")
    monkeypatch.setenv("MCP_GH_ALLOW_RELEASE_CREATION", "false")
    monkeypatch.setenv("MCP_GH_ALLOW_WORKFLOW_DISPATCH", "false")
    monkeypatch.setenv("MCP_GH_ALLOWED_REPOSITORIES", "octo/repo")
    monkeypatch.setenv("MCP_GH_TRANSPORT", "streamable-http")
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
            review = await session.call_tool(
                "gh_submit_pr_review",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "number": 224,
                    "expected_head_sha": "b" * 40,
                    "action": "approve",
                    "body": "Reviewed exact revision.",
                },
                allow_input_required=True,
            )
            assert not isinstance(review, InputRequiredResult)
            assert review.is_error is False
            assert review.structured_content["state"] == "APPROVED"

            info = await session.call_tool("gh_server_info", {})
            assert info.is_error is False
            assert info.structured_content["pr_merge_enabled"] is True
            assert info.structured_content["repo_creation_enabled"] is False
            assert info.structured_content["release_creation_enabled"] is False
            assert info.structured_content["workflow_dispatch_enabled"] is False

            merge = await session.call_tool(
                "gh_merge_pr",
                {
                    "owner": "octo",
                    "repo": "repo",
                    "number": 224,
                    "expected_head_sha": "b" * 40,
                    "method": "squash",
                    "body": "Merge exact reviewed revision.",
                },
                allow_input_required=True,
            )
            assert not isinstance(merge, InputRequiredResult)
            assert merge.is_error is False
            assert merge.structured_content["merged"] is True
            assert len((await session.list_tools()).tools) == 58
    finally:
        get_settings.cache_clear()
