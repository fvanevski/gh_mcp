"""Exact public MCP tool-surface regression snapshot."""

from __future__ import annotations

import pytest

from mcp_gh_server.server import mcp

EXPECTED_SURFACE: dict[str, tuple[set[str], set[str]]] = {
    "gh_server_info": (set(), set()),
    "gh_info": (set(), set()),
    "gh_search_repos": (
        {"query", "sort", "order", "per_page"},
        {"query"},
    ),
    "gh_search_issues": (
        {"query", "sort", "order", "per_page"},
        {"query"},
    ),
    "gh_search_code": ({"query", "per_page"}, {"query"}),
    "gh_list_issues": (
        {"owner", "repo", "state", "per_page", "labels"},
        {"owner", "repo"},
    ),
    "gh_get_issue": ({"owner", "repo", "number"}, {"owner", "repo", "number"}),
    "gh_create_issue": (
        {"owner", "repo", "title", "body", "labels", "assignees"},
        {"owner", "repo", "title"},
    ),
    "gh_set_issue_state": (
        {"owner", "repo", "number", "expected_state", "new_state", "state_reason"},
        {"owner", "repo", "number", "expected_state", "new_state", "state_reason"},
    ),
    "gh_set_pr_draft_state": (
        {
            "owner",
            "repo",
            "number",
            "expected_head_sha",
            "expected_is_draft",
            "new_is_draft",
        },
        {
            "owner",
            "repo",
            "number",
            "expected_head_sha",
            "expected_is_draft",
            "new_is_draft",
        },
    ),
    "gh_list_prs": (
        {"owner", "repo", "state", "per_page"},
        {"owner", "repo"},
    ),
    "gh_get_pr": ({"owner", "repo", "number"}, {"owner", "repo", "number"}),
    "gh_get_pr_diff": (
        {"owner", "repo", "number", "format", "max_bytes"},
        {"owner", "repo", "number"},
    ),
    "gh_list_pr_files": (
        {"owner", "repo", "number", "page", "per_page"},
        {"owner", "repo", "number"},
    ),
    "gh_list_pr_commits": (
        {"owner", "repo", "number", "page", "per_page"},
        {"owner", "repo", "number"},
    ),
    "gh_list_pr_reviews": (
        {"owner", "repo", "number", "page", "per_page"},
        {"owner", "repo", "number"},
    ),
    "gh_get_pr_review_state": (
        {"owner", "repo", "number", "expected_head_sha"},
        {"owner", "repo", "number", "expected_head_sha"},
    ),
    "gh_get_pr_checks": (
        {"owner", "repo", "number", "required_only", "max_checks"},
        {"owner", "repo", "number"},
    ),
    "gh_submit_pr_review": (
        {"owner", "repo", "number", "expected_head_sha", "action", "body"},
        {"owner", "repo", "number", "expected_head_sha", "action"},
    ),
    "gh_merge_pr": (
        {"owner", "repo", "number", "expected_head_sha", "method", "subject", "body"},
        {"owner", "repo", "number", "expected_head_sha", "method"},
    ),
    "gh_create_pr": (
        {
            "owner",
            "repo",
            "title",
            "body",
            "head",
            "base",
            "draft",
            "labels",
            "assignees",
            "review_users",
        },
        {"owner", "repo", "title", "body", "head", "base"},
    ),
    "gh_get_repo": ({"owner", "repo"}, {"owner", "repo"}),
    "gh_list_repos": (
        {"username", "type", "per_page", "sort", "direction"},
        set(),
    ),
    "gh_get_file_contents": (
        {"owner", "repo", "path", "ref"},
        {"owner", "repo", "path", "ref"},
    ),
    "gh_get_ref": (
        {"owner", "repo", "ref"},
        {"owner", "repo", "ref"},
    ),
    "gh_get_commit": (
        {"owner", "repo", "commit_sha"},
        {"owner", "repo", "commit_sha"},
    ),
    "gh_commit_files": (
        {"owner", "repo", "branch", "expected_head_sha", "files", "commit_message"},
        {"owner", "repo", "branch", "expected_head_sha", "files", "commit_message"},
    ),
    "gh_create_repo": (
        {"name", "description", "private", "auto_init"},
        {"name"},
    ),
    "gh_list_releases": (
        {"owner", "repo", "per_page"},
        {"owner", "repo"},
    ),
    "gh_get_release": (
        {"owner", "repo", "tag"},
        {"owner", "repo", "tag"},
    ),
    "gh_create_release": (
        {"owner", "repo", "tag_name", "name", "body", "draft", "prerelease", "target"},
        {"owner", "repo", "tag_name"},
    ),
    "gh_list_workflows": (
        {"owner", "repo", "state", "per_page"},
        {"owner", "repo"},
    ),
    "gh_get_workflow": (
        {"owner", "repo", "workflow_id"},
        {"owner", "repo", "workflow_id"},
    ),
    "gh_run_workflow": (
        {"owner", "repo", "workflow_id", "ref", "fields"},
        {"owner", "repo", "workflow_id"},
    ),
    "gh_list_runs": (
        {
            "owner",
            "repo",
            "branch",
            "status",
            "per_page",
            "workflow_id",
            "head_sha",
            "event",
            "actor",
            "created_from",
            "created_to",
            "check_suite_id",
            "page",
        },
        {"owner", "repo"},
    ),
    "gh_get_run": (
        {"owner", "repo", "run_id"},
        {"owner", "repo", "run_id"},
    ),
    "gh_list_run_artifacts": (
        {"owner", "repo", "run_id", "page", "per_page", "name"},
        {"owner", "repo", "run_id"},
    ),
    "gh_get_artifact": (
        {"owner", "repo", "artifact_id"},
        {"owner", "repo", "artifact_id"},
    ),
    "gh_list_run_jobs": (
        {"owner", "repo", "run_id", "attempt", "page", "per_page"},
        {"owner", "repo", "run_id"},
    ),
    "gh_get_failed_run_logs": (
        {"owner", "repo", "run_id", "attempt", "max_bytes"},
        {"owner", "repo", "run_id"},
    ),
    "gh_get_job_logs": (
        {
            "owner",
            "repo",
            "job_id",
            "attempt",
            "max_bytes",
            "tail_bytes",
            "start_marker",
            "end_marker",
        },
        {"owner", "repo", "job_id", "attempt"},
    ),
    "gh_get_run_logs": (
        {
            "owner",
            "repo",
            "run_id",
            "attempt",
            "max_bytes",
            "tail_bytes",
            "start_marker",
            "end_marker",
        },
        {"owner", "repo", "run_id", "attempt"},
    ),
    "gh_watch_run": (
        {"owner", "repo", "run_id", "interval", "exit_status", "timeout_seconds"},
        {"owner", "repo", "run_id"},
    ),
    "gh_edit_issue": (
        {
            "owner",
            "repo",
            "number",
            "title",
            "body",
            "labels_add",
            "labels_remove",
            "assignees_add",
            "assignees_remove",
            "milestone",
            "remove_milestone",
        },
        {"owner", "repo", "number"},
    ),
    "gh_list_labels": (
        {"owner", "repo", "per_page"},
        {"owner", "repo"},
    ),
    "gh_create_label": (
        {"owner", "repo", "name", "color", "description"},
        {"owner", "repo", "name", "color"},
    ),
    "gh_upsert_label": (
        {"owner", "repo", "name", "color", "description"},
        {"owner", "repo", "name", "color"},
    ),
    "gh_edit_label": (
        {"owner", "repo", "name", "new_name", "color", "description"},
        {"owner", "repo", "name"},
    ),
    "gh_list_milestones": (
        {"owner", "repo", "state", "per_page"},
        {"owner", "repo"},
    ),
    "gh_create_milestone": (
        {"owner", "repo", "title", "description", "due_on", "state"},
        {"owner", "repo", "title"},
    ),
    "gh_create_comment": (
        {"owner", "repo", "issue_number", "body"},
        {"owner", "repo", "issue_number", "body"},
    ),
    "gh_create_branch": (
        {"owner", "repo", "issue_number", "name", "base"},
        {"owner", "repo", "issue_number", "name"},
    ),
    "gh_create_branch_from_sha": (
        {"owner", "repo", "name", "base_sha"},
        {"owner", "repo", "name", "base_sha"},
    ),
    "gh_edit_pr": (
        {
            "owner",
            "repo",
            "number",
            "title",
            "body",
            "labels_add",
            "labels_remove",
            "assignees_add",
            "assignees_remove",
            "base",
        },
        {"owner", "repo", "number"},
    ),
}

READ_ONLY_TOOLS = {
    "gh_server_info",
    "gh_info",
    "gh_search_repos",
    "gh_search_issues",
    "gh_search_code",
    "gh_list_issues",
    "gh_get_issue",
    "gh_list_prs",
    "gh_get_pr",
    "gh_get_pr_diff",
    "gh_list_pr_files",
    "gh_list_pr_commits",
    "gh_list_pr_reviews",
    "gh_get_pr_review_state",
    "gh_get_pr_checks",
    "gh_get_repo",
    "gh_list_repos",
    "gh_get_file_contents",
    "gh_get_ref",
    "gh_get_commit",
    "gh_list_releases",
    "gh_get_release",
    "gh_list_workflows",
    "gh_get_workflow",
    "gh_list_runs",
    "gh_get_run",
    "gh_list_run_artifacts",
    "gh_get_artifact",
    "gh_watch_run",
    "gh_list_run_jobs",
    "gh_get_failed_run_logs",
    "gh_get_job_logs",
    "gh_get_run_logs",
    "gh_list_labels",
    "gh_list_milestones",
}

DESTRUCTIVE_WRITE_TOOLS = {
    "gh_merge_pr",
    "gh_commit_files",
    "gh_run_workflow",
    "gh_edit_issue",
    "gh_set_issue_state",
    "gh_set_pr_draft_state",
    "gh_upsert_label",
    "gh_edit_label",
    "gh_edit_pr",
}

EXPECTED_DESCRIPTIONS = {
    "gh_create_issue": (
        "Create a new issue in a repository.\n\n"
        "This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host\n"
        "is responsible for user-facing approval."
    ),
    "gh_edit_issue": (
        "Edit an existing issue in a repository.\n\n"
        "This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host\n"
        "is responsible for user-facing approval."
    ),
    "gh_list_milestones": (
        "List milestones in a repository via the GitHub API.\n\n"
        "state: open, closed, or all (default: all)."
    ),
    "gh_create_milestone": (
        "Create a new milestone in a repository via the GitHub API.\n\n"
        "due_on: due date in ISO format (e.g. '2026-12-31').\n"
        "state: open or closed (default: open).\n\n"
        "This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host\n"
        "is responsible for user-facing approval."
    ),
}


@pytest.mark.asyncio
async def test_exact_tool_surface_snapshot() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert len(tools) == 54
    assert set(tools) == set(EXPECTED_SURFACE)

    for name, tool in tools.items():
        properties, required = EXPECTED_SURFACE[name]
        assert set(tool.input_schema["properties"]) == properties
        assert set(tool.input_schema.get("required", [])) == required
        assert tool.annotations.read_only_hint is (name in READ_ONLY_TOOLS)
        assert tool.annotations.destructive_hint is (name in DESTRUCTIVE_WRITE_TOOLS)
        assert tool.annotations.idempotent_hint is (name in READ_ONLY_TOOLS)
        assert tool.annotations.open_world_hint is (name != "gh_server_info")

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
    issue_state_output = tools["gh_set_issue_state"].output_schema["properties"]
    assert {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
        "previous_state",
        "new_state",
        "state_reason",
        "closed_at",
        "reopened_at",
    } <= set(issue_state_output)

    draft_state_schema = tools["gh_set_pr_draft_state"].input_schema["properties"]
    assert draft_state_schema["number"]["minimum"] == 1
    assert draft_state_schema["expected_head_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    assert draft_state_schema["expected_is_draft"]["type"] == "boolean"
    assert draft_state_schema["new_is_draft"]["type"] == "boolean"
    draft_state_output = tools["gh_set_pr_draft_state"].output_schema["properties"]
    assert {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
        "number",
        "previous_head_sha",
        "current_head_sha",
        "previous_is_draft",
        "current_is_draft",
        "url",
    } == set(draft_state_output)

    reviews_schema = tools["gh_list_pr_reviews"].input_schema["properties"]
    assert reviews_schema["number"]["minimum"] == 1
    assert reviews_schema["page"]["minimum"] == 1
    assert reviews_schema["per_page"]["anyOf"][0]["maximum"] == 100
    reviews_output = tools["gh_list_pr_reviews"].output_schema["properties"]
    assert {
        "number",
        "base_sha",
        "head_sha",
        "page",
        "per_page",
        "returned_count",
        "has_more",
        "truncated",
        "warning",
        "reviews",
    } <= set(reviews_output)

    review_state_schema = tools["gh_get_pr_review_state"].input_schema["properties"]
    assert review_state_schema["number"]["minimum"] == 1
    assert review_state_schema["expected_head_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    review_state_output = tools["gh_get_pr_review_state"].output_schema["properties"]
    assert review_state_output["head_matches_expected"]["type"] == "boolean"
    assert review_state_output["exact_head_evidence"]["type"] == "boolean"
    assert {
        "current_head_approvals",
        "current_head_change_requests",
        "current_head_comments",
        "stale_approvals",
        "stale_change_requests",
        "requested_reviewers",
        "requested_teams",
        "unresolved_review_threads",
        "requirements_satisfied",
    } <= set(review_state_output)

    runs_schema = tools["gh_list_runs"].input_schema["properties"]
    assert runs_schema["workflow_id"]["anyOf"][0]["minimum"] == 1
    assert runs_schema["head_sha"]["anyOf"][0]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    assert runs_schema["check_suite_id"]["anyOf"][0]["minimum"] == 1
    assert runs_schema["page"]["minimum"] == 1
    runs_output = tools["gh_list_runs"].output_schema["properties"]
    assert {"total_count", "page", "per_page", "has_more", "truncated", "warning"} <= set(
        runs_output
    )

    artifacts_schema = tools["gh_list_run_artifacts"].input_schema["properties"]
    assert artifacts_schema["run_id"]["minimum"] == 1
    assert artifacts_schema["page"]["minimum"] == 1
    assert artifacts_schema["per_page"]["anyOf"][0]["maximum"] == 100
    assert artifacts_schema["name"]["anyOf"][0]["minLength"] == 1
    artifacts_output = tools["gh_list_run_artifacts"].output_schema["properties"]
    assert {
        "run_id",
        "attempt",
        "head_sha",
        "total_count",
        "page",
        "per_page",
        "has_more",
        "truncated",
        "warning",
        "artifacts",
    } <= set(artifacts_output)

    artifact_schema = tools["gh_get_artifact"].input_schema["properties"]
    assert artifact_schema["artifact_id"]["minimum"] == 1
    artifact_output = tools["gh_get_artifact"].output_schema["properties"]
    assert artifact_output["workflow_head_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert artifact_output["expired"]["type"] == "boolean"

    for name in ("gh_get_job_logs", "gh_get_run_logs"):
        schema = tools[name].input_schema["properties"]
        assert schema["attempt"]["minimum"] == 1
        assert schema["max_bytes"]["anyOf"][0]["maximum"] == 1_000_000
        assert schema["tail_bytes"]["anyOf"][0]["maximum"] == 1_000_000
        assert schema["start_marker"]["anyOf"][0]["minLength"] == 1
        output = tools[name].output_schema["properties"]
        assert output["text"]["type"] == "string"
        assert output["truncated"]["type"] == "boolean"
        assert output["sha256"]["pattern"] == r"^[0-9a-f]{64}$"
    assert tools["gh_get_job_logs"].input_schema["properties"]["job_id"]["minimum"] == 1
    assert tools["gh_get_run_logs"].input_schema["properties"]["run_id"]["minimum"] == 1

    for name, description in EXPECTED_DESCRIPTIONS.items():
        assert tools[name].description == description
