"""Exact public MCP tool-surface regression snapshot."""

from __future__ import annotations

import pytest

from mcp_gh_server.server import mcp

EXPECTED_INPUT_PROPERTIES: dict[str, set[str]] = {
    "gh_server_info": set(),
    "gh_info": set(),
    "gh_search_repos": {"query", "sort", "order", "per_page"},
    "gh_search_issues": {"query", "sort", "order", "per_page"},
    "gh_search_code": {"query", "per_page"},
    "gh_list_issues": {"owner", "repo", "state", "per_page", "labels"},
    "gh_get_issue": {"owner", "repo", "number"},
    "gh_create_issue": {"owner", "repo", "title", "body", "labels", "assignees"},
    "gh_list_prs": {"owner", "repo", "state", "per_page"},
    "gh_get_pr": {"owner", "repo", "number"},
    "gh_get_pr_diff": {"owner", "repo", "number", "format", "max_bytes"},
    "gh_list_pr_files": {"owner", "repo", "number", "page", "per_page"},
    "gh_list_pr_commits": {"owner", "repo", "number", "page", "per_page"},
    "gh_get_pr_checks": {"owner", "repo", "number", "required_only", "max_checks"},
    "gh_submit_pr_review": {"owner", "repo", "number", "expected_head_sha", "action", "body"},
    "gh_merge_pr": {"owner", "repo", "number", "expected_head_sha", "method", "subject", "body"},
    "gh_create_pr": {"owner", "repo", "title", "body", "head", "base", "draft", "labels", "assignees", "review_users"},
    "gh_get_repo": {"owner", "repo"},
    "gh_list_repos": {"username", "type", "per_page", "sort", "direction"},
    "gh_get_file_contents": {"owner", "repo", "path", "ref"},
    "gh_commit_files": {"owner", "repo", "branch", "expected_head_sha", "files", "commit_message"},
    "gh_create_repo": {"name", "description", "private", "auto_init"},
    "gh_list_releases": {"owner", "repo", "per_page"},
    "gh_get_release": {"owner", "repo", "tag"},
    "gh_create_release": {"owner", "repo", "tag_name", "name", "body", "draft", "prerelease", "target"},
    "gh_list_workflows": {"owner", "repo", "state", "per_page"},
    "gh_get_workflow": {"owner", "repo", "workflow_id"},
    "gh_run_workflow": {"owner", "repo", "workflow_id", "ref", "fields"},
    "gh_list_runs": {"owner", "repo", "branch", "status", "per_page"},
    "gh_get_run": {"owner", "repo", "run_id"},
    "gh_list_run_jobs": {"owner", "repo", "run_id", "attempt", "page", "per_page"},
    "gh_get_failed_run_logs": {"owner", "repo", "run_id", "attempt", "max_bytes"},
    "gh_watch_run": {"owner", "repo", "run_id", "interval", "exit_status", "timeout_seconds"},
    "gh_edit_issue": {"owner", "repo", "number", "title", "body", "labels_add", "labels_remove", "assignees_add", "assignees_remove", "milestone", "remove_milestone"},
    "gh_list_labels": {"owner", "repo", "per_page"},
    "gh_create_label": {"owner", "repo", "name", "color", "description"},
    "gh_upsert_label": {"owner", "repo", "name", "color", "description"},
    "gh_edit_label": {"owner", "repo", "name", "new_name", "color", "description"},
    "gh_list_milestones": {"owner", "repo", "state", "per_page"},
    "gh_create_milestone": {"owner", "repo", "title", "description", "due_on", "state"},
    "gh_create_comment": {"owner", "repo", "issue_number", "body"},
    "gh_create_branch": {"owner", "repo", "issue_number", "name", "base"},
    "gh_create_branch_from_sha": {"owner", "repo", "name", "base_sha"},
    "gh_edit_pr": {"owner", "repo", "number", "title", "body", "labels_add", "labels_remove", "assignees_add", "assignees_remove", "base"},
}

EXPECTED_REQUIRED: dict[str, set[str]] = {
    "gh_server_info": set(), "gh_info": set(),
    "gh_search_repos": {"query"}, "gh_search_issues": {"query"}, "gh_search_code": {"query"},
    "gh_list_issues": {"owner", "repo"}, "gh_get_issue": {"owner", "repo", "number"},
    "gh_create_issue": {"owner", "repo", "title"}, "gh_list_prs": {"owner", "repo"},
    "gh_get_pr": {"owner", "repo", "number"}, "gh_get_pr_diff": {"owner", "repo", "number"},
    "gh_list_pr_files": {"owner", "repo", "number"}, "gh_list_pr_commits": {"owner", "repo", "number"},
    "gh_get_pr_checks": {"owner", "repo", "number"},
    "gh_submit_pr_review": {"owner", "repo", "number", "expected_head_sha", "action"},
    "gh_merge_pr": {"owner", "repo", "number", "expected_head_sha", "method"},
    "gh_create_pr": {"owner", "repo", "title", "body", "head", "base"},
    "gh_get_repo": {"owner", "repo"}, "gh_list_repos": set(),
    "gh_get_file_contents": {"owner", "repo", "path", "ref"},
    "gh_commit_files": {"owner", "repo", "branch", "expected_head_sha", "files", "commit_message"},
    "gh_create_repo": {"name"}, "gh_list_releases": {"owner", "repo"},
    "gh_get_release": {"owner", "repo", "tag"}, "gh_create_release": {"owner", "repo", "tag_name"},
    "gh_list_workflows": {"owner", "repo"}, "gh_get_workflow": {"owner", "repo", "workflow_id"},
    "gh_run_workflow": {"owner", "repo", "workflow_id"}, "gh_list_runs": {"owner", "repo"},
    "gh_get_run": {"owner", "repo", "run_id"}, "gh_list_run_jobs": {"owner", "repo", "run_id"},
    "gh_get_failed_run_logs": {"owner", "repo", "run_id"}, "gh_watch_run": {"owner", "repo", "run_id"},
    "gh_edit_issue": {"owner", "repo", "number"}, "gh_list_labels": {"owner", "repo"},
    "gh_create_label": {"owner", "repo", "name", "color"},
    "gh_upsert_label": {"owner", "repo", "name", "color"},
    "gh_edit_label": {"owner", "repo", "name"}, "gh_list_milestones": {"owner", "repo"},
    "gh_create_milestone": {"owner", "repo", "title"},
    "gh_create_comment": {"owner", "repo", "issue_number", "body"},
    "gh_create_branch": {"owner", "repo", "issue_number", "name"},
    "gh_create_branch_from_sha": {"owner", "repo", "name", "base_sha"},
    "gh_edit_pr": {"owner", "repo", "number"},
}

READ_ONLY_TOOLS = {
    "gh_server_info", "gh_info", "gh_search_repos", "gh_search_issues", "gh_search_code",
    "gh_list_issues", "gh_get_issue", "gh_list_prs", "gh_get_pr", "gh_get_pr_diff",
    "gh_list_pr_files", "gh_list_pr_commits", "gh_get_pr_checks", "gh_get_repo", "gh_list_repos",
    "gh_get_file_contents", "gh_list_releases", "gh_get_release", "gh_list_workflows", "gh_get_workflow",
    "gh_list_runs", "gh_get_run", "gh_watch_run", "gh_list_run_jobs", "gh_get_failed_run_logs",
    "gh_list_labels", "gh_list_milestones",
}
DESTRUCTIVE_WRITE_TOOLS = {
    "gh_merge_pr", "gh_commit_files", "gh_run_workflow", "gh_edit_issue",
    "gh_upsert_label", "gh_edit_label", "gh_edit_pr",
}


@pytest.mark.asyncio
async def test_exact_tool_surface_snapshot() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert len(tools) == 44
    assert set(tools) == set(EXPECTED_INPUT_PROPERTIES)
    assert set(EXPECTED_REQUIRED) == set(EXPECTED_INPUT_PROPERTIES)

    for name, tool in tools.items():
        assert set(tool.input_schema["properties"]) == EXPECTED_INPUT_PROPERTIES[name]
        assert set(tool.input_schema.get("required", [])) == EXPECTED_REQUIRED[name]
        assert tool.annotations.read_only_hint is (name in READ_ONLY_TOOLS)
        assert tool.annotations.destructive_hint is (name in DESTRUCTIVE_WRITE_TOOLS)
        assert tool.annotations.idempotent_hint is (name in READ_ONLY_TOOLS)
        assert tool.annotations.open_world_hint is (name != "gh_server_info")
