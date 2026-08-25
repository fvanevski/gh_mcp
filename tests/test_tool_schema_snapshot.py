"""Exact public MCP tool-surface regression snapshot."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_gh_server.server import mcp

EXPECTED_SURFACE: dict[str, tuple[set[str], set[str]]] = {
    "gh_server_info": (set(), set()),
    "gh_info": (set(), set()),
    "gh_get_api_rate_status": (set(), set()),
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
    "gh_get_merge_requirements": (
        {"owner", "repo", "number", "expected_head_sha"},
        {"owner", "repo", "number", "expected_head_sha"},
    ),
    "gh_get_pr_checks": (
        {"owner", "repo", "number", "required_only", "max_checks"},
        {"owner", "repo", "number"},
    ),
    "gh_get_pr_review_eligibility": (
        {"owner", "repo", "number", "expected_head_sha"},
        {"owner", "repo", "number", "expected_head_sha"},
    ),
    "gh_approve_pr": (
        {"owner", "repo", "number", "expected_head_sha", "expected_reviewer_login", "body"},
        {"owner", "repo", "number", "expected_head_sha", "expected_reviewer_login"},
    ),
    "gh_request_pr_changes": (
        {"owner", "repo", "number", "expected_head_sha", "expected_reviewer_login", "body"},
        {"owner", "repo", "number", "expected_head_sha", "expected_reviewer_login", "body"},
    ),
    "gh_comment_pr_review": (
        {"owner", "repo", "number", "expected_head_sha", "body"},
        {"owner", "repo", "number", "expected_head_sha", "body"},
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
    "gh_compare_commits": (
        {"owner", "repo", "base_sha", "head_sha", "max_commits", "max_files"},
        {"owner", "repo", "base_sha", "head_sha"},
    ),
    "gh_commit_files": (
        {"owner", "repo", "branch", "expected_head_sha", "files", "commit_message"},
        {"owner", "repo", "branch", "expected_head_sha", "files", "commit_message"},
    ),
    "gh_patch_files": (
        {"owner", "repo", "branch", "expected_head_sha", "patches", "commit_message"},
        {"owner", "repo", "branch", "expected_head_sha", "patches", "commit_message"},
    ),
    "gh_create_repo": (
        {"owner", "repo", "description", "private", "auto_init"},
        {"owner", "repo"},
    ),
    "gh_list_releases": (
        {"owner", "repo", "per_page"},
        {"owner", "repo"},
    ),
    "gh_get_release": (
        {"owner", "repo", "tag"},
        {"owner", "repo", "tag"},
    ),
    "gh_create_release_exact": (
        {
            "owner",
            "repo",
            "tag_name",
            "expected_target_sha",
            "make_latest",
            "name",
            "body",
            "draft",
            "prerelease",
            "expected_tag_absent",
            "expected_release_absent",
        },
        {"owner", "repo", "tag_name", "expected_target_sha", "make_latest"},
    ),
    "gh_list_workflows": (
        {"owner", "repo", "state", "per_page"},
        {"owner", "repo"},
    ),
    "gh_get_workflow": (
        {"owner", "repo", "workflow_id"},
        {"owner", "repo", "workflow_id"},
    ),
    "gh_run_workflow_exact": (
        {
            "owner",
            "repo",
            "workflow_id",
            "expected_workflow_path",
            "ref",
            "expected_ref_sha",
            "inputs",
        },
        {"owner", "repo", "workflow_id", "expected_workflow_path", "ref", "expected_ref_sha"},
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
    "gh_list_artifact_files": (
        {"owner", "repo", "artifact_id", "page", "per_page"},
        {"owner", "repo", "artifact_id"},
    ),
    "gh_read_artifact_file": (
        {"owner", "repo", "artifact_id", "path", "max_bytes"},
        {"owner", "repo", "artifact_id", "path"},
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
    "gh_get_api_rate_status",
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
    "gh_get_pr_review_eligibility",
    "gh_get_merge_requirements",
    "gh_get_pr_checks",
    "gh_get_repo",
    "gh_list_repos",
    "gh_get_file_contents",
    "gh_get_ref",
    "gh_get_commit",
    "gh_compare_commits",
    "gh_list_releases",
    "gh_get_release",
    "gh_list_workflows",
    "gh_get_workflow",
    "gh_list_runs",
    "gh_get_run",
    "gh_list_run_artifacts",
    "gh_get_artifact",
    "gh_list_artifact_files",
    "gh_read_artifact_file",
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
    "gh_patch_files",
    "gh_run_workflow_exact",
    "gh_edit_issue",
    "gh_set_issue_state",
    "gh_set_pr_draft_state",
    "gh_edit_label",
    "gh_edit_pr",
}

EXPECTED_DESCRIPTIONS = {
    "gh_create_issue": (
        "Additive write: create exactly one issue in the target repository. "
        "The ordinary write gate and repository policy must allow the target. "
        "Optional labels and assignees are bounded; one mutation attempt is followed "
        "by authoritative semantic readback when stable identity is available. The "
        "tool never retries an ambiguous mutation automatically and does not edit, "
        "close, comment on, or delete an existing issue."
    ),
    "gh_edit_issue": (
        "Destructive write: edit metadata on exactly one existing issue after "
        "ordinary write authorization. The request may change title, body, labels, "
        "assignees, or milestone; one mutation attempt is followed by authoritative "
        "semantic readback of the requested fields. Ambiguous mutations are never "
        "retried automatically. It does not close or reopen the issue, post comments, "
        "delete the issue, or bypass repository policy."
    ),
    "gh_list_milestones": (
        "List milestones in a repository via the GitHub API.\n\n"
        "state: open, closed, or all (default: all)."
    ),
    "gh_create_milestone": (
        "Additive write: create exactly one repository milestone with bounded title, "
        "description, due date, and explicit open/closed state after ordinary write "
        "authorization. One mutation attempt is followed by authoritative readback "
        "of the stable milestone number and requested fields; ambiguous creation is "
        "never retried automatically. It does not assign issues to the milestone or "
        "edit existing milestones."
    ),
}

EXACT_OUTCOME_FIELDS = {
    "precondition_checked",
    "write_completed",
    "readback_completed",
    "state_matches_requested",
    "warning",
    "request_id",
}


def _output_properties(tool: Any) -> dict[str, Any]:
    output_schema = tool.output_schema
    assert output_schema is not None
    properties = output_schema.get("properties")
    assert isinstance(properties, dict)
    return properties


@pytest.mark.asyncio
async def test_exact_tool_surface_snapshot() -> None:
    tools: dict[str, Any] = {tool.name: tool for tool in await mcp.list_tools()}

    assert len(tools) == 62
    assert set(tools) == set(EXPECTED_SURFACE)
    assert "gh_create_release" not in tools
    assert "gh_upsert_label" not in tools
    assert "gh_submit_pr_review" not in tools

    for name, tool in tools.items():
        properties, required = EXPECTED_SURFACE[name]
        assert set(tool.input_schema["properties"]) == properties
        assert set(tool.input_schema.get("required", [])) == required
        assert tool.annotations.read_only_hint is (name in READ_ONLY_TOOLS)
        assert tool.annotations.destructive_hint is (name in DESTRUCTIVE_WRITE_TOOLS)
        assert tool.annotations.idempotent_hint is (name in READ_ONLY_TOOLS)
        assert tool.annotations.open_world_hint is (name != "gh_server_info")

    for name in (
        "gh_create_issue",
        "gh_edit_issue",
        "gh_create_label",
        "gh_edit_label",
        "gh_create_milestone",
    ):
        assert set(_output_properties(tools[name])) >= EXACT_OUTCOME_FIELDS

    rate_output = _output_properties(tools["gh_get_api_rate_status"])
    assert {"github", "governor"} == set(rate_output)

    repo_create_schema = tools["gh_create_repo"].input_schema["properties"]
    assert "name" not in repo_create_schema
    assert repo_create_schema["owner"]["pattern"] == (r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
    assert repo_create_schema["owner"]["maxLength"] == 39
    assert repo_create_schema["repo"]["pattern"] == r"^[A-Za-z0-9_.-]{1,100}$"
    assert repo_create_schema["repo"]["maxLength"] == 100
    repo_create_output = _output_properties(tools["gh_create_repo"])
    assert {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
        "owner",
        "repo",
        "name_with_owner",
        "url",
        "is_private",
        "description",
        "initialized",
    } == set(repo_create_output)

    compare_schema = tools["gh_compare_commits"].input_schema["properties"]
    assert compare_schema["base_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    assert compare_schema["head_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    assert compare_schema["max_commits"]["anyOf"][0]["minimum"] == 1
    assert compare_schema["max_files"]["anyOf"][0]["minimum"] == 1
    compare_output = _output_properties(tools["gh_compare_commits"])
    assert {
        "base_sha",
        "head_sha",
        "base_found",
        "head_found",
        "comparison_available",
        "merge_base_sha",
        "status",
        "ahead_by",
        "behind_by",
        "total_commits",
        "commits",
        "commits_evidence",
        "files",
        "files_evidence",
        "truncated",
        "evidence_complete",
        "sha256",
        "warning",
    } == set(compare_output)
    assert compare_output["base_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert compare_output["head_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert compare_output["truncated"]["type"] == "boolean"
    assert compare_output["evidence_complete"]["type"] == "boolean"
    assert compare_output["sha256"]["pattern"] == r"^[0-9a-f]{64}$"

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
    issue_state_output = _output_properties(tools["gh_set_issue_state"])
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
    draft_state_output = _output_properties(tools["gh_set_pr_draft_state"])
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

    exact_release_schema = tools["gh_create_release_exact"].input_schema["properties"]
    assert exact_release_schema["tag_name"]["maxLength"] == 1019
    assert exact_release_schema["expected_target_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    assert exact_release_schema["make_latest"]["type"] == "boolean"
    assert exact_release_schema["expected_tag_absent"]["default"] is True
    assert exact_release_schema["expected_release_absent"]["default"] is True
    exact_release_output = _output_properties(tools["gh_create_release_exact"])
    assert {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
        "tag_name",
        "expected_target_sha",
        "resolved_target_sha",
        "release_id",
        "release_url",
        "tag_commit_sha",
        "release_name",
        "is_draft",
        "is_prerelease",
        "make_latest",
        "is_latest",
    } == set(exact_release_output)

    exact_dispatch_schema = tools["gh_run_workflow_exact"].input_schema["properties"]
    assert exact_dispatch_schema["workflow_id"]["minimum"] == 1
    assert exact_dispatch_schema["expected_workflow_path"]["pattern"] == (
        r"^\.github/workflows/[^/\x00-\x1f\x7f]+\.ya?ml$"
    )
    assert exact_dispatch_schema["ref"]["pattern"] == r"^(?:heads|tags)/.+$"
    assert exact_dispatch_schema["expected_ref_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    inputs_schema = exact_dispatch_schema["inputs"]["anyOf"][0]
    assert inputs_schema["type"] == "object"
    assert inputs_schema["maxProperties"] == 25
    assert inputs_schema["propertyNames"]["minLength"] == 1
    assert inputs_schema["additionalProperties"]["type"] == "string"
    exact_dispatch_output = _output_properties(tools["gh_run_workflow_exact"])
    assert {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
        "workflow_id",
        "ref",
        "expected_ref_sha",
        "resolved_ref_sha",
        "matching_run_count",
        "run_id",
        "run_url",
        "run_status",
        "run_head_sha",
        "run_event",
    } == set(exact_dispatch_output)

    reviews_schema = tools["gh_list_pr_reviews"].input_schema["properties"]
    assert reviews_schema["number"]["minimum"] == 1
    assert reviews_schema["page"]["minimum"] == 1
    assert reviews_schema["per_page"]["anyOf"][0]["maximum"] == 100
    reviews_output = _output_properties(tools["gh_list_pr_reviews"])
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
    review_state_output = _output_properties(tools["gh_get_pr_review_state"])
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

    merge_requirements_schema = tools["gh_get_merge_requirements"].input_schema["properties"]
    assert merge_requirements_schema["number"]["minimum"] == 1
    assert merge_requirements_schema["expected_head_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    merge_requirements_output = _output_properties(tools["gh_get_merge_requirements"])
    assert {
        "base_ref",
        "base_sha",
        "expected_head_sha",
        "current_head_sha",
        "head_matches_expected",
        "exact_head_evidence",
        "mergeable",
        "merge_state",
        "policy_evidence_complete",
        "checks_evidence_complete",
        "review_evidence_complete",
        "up_to_date_evidence_complete",
        "required_status_checks",
        "current_required_checks",
        "required_approvals",
        "current_valid_approvals",
        "current_valid_approval_count",
        "code_owner_review_required",
        "last_push_approval_required",
        "conversation_resolution_required",
        "unresolved_review_threads",
        "up_to_date_required",
        "up_to_date",
        "allowed_merge_methods",
        "allowed_merge_methods_complete",
        "warning",
    } <= set(merge_requirements_output)

    runs_schema = tools["gh_list_runs"].input_schema["properties"]
    assert runs_schema["workflow_id"]["anyOf"][0]["minimum"] == 1
    assert runs_schema["head_sha"]["anyOf"][0]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    assert runs_schema["check_suite_id"]["anyOf"][0]["minimum"] == 1
    assert runs_schema["page"]["minimum"] == 1
    runs_output = _output_properties(tools["gh_list_runs"])
    assert {"total_count", "page", "per_page", "has_more", "truncated", "warning"} <= set(
        runs_output
    )

    artifacts_schema = tools["gh_list_run_artifacts"].input_schema["properties"]
    assert artifacts_schema["run_id"]["minimum"] == 1
    assert artifacts_schema["page"]["minimum"] == 1
    assert artifacts_schema["per_page"]["anyOf"][0]["maximum"] == 100
    assert artifacts_schema["name"]["anyOf"][0]["minLength"] == 1
    artifacts_output = _output_properties(tools["gh_list_run_artifacts"])
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
    artifact_output = _output_properties(tools["gh_get_artifact"])
    assert artifact_output["workflow_head_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert artifact_output["expired"]["type"] == "boolean"

    artifact_files_schema = tools["gh_list_artifact_files"].input_schema["properties"]
    assert artifact_files_schema["artifact_id"]["minimum"] == 1
    assert artifact_files_schema["page"]["minimum"] == 1
    assert artifact_files_schema["per_page"]["anyOf"][0]["maximum"] == 100
    artifact_files_output = _output_properties(tools["gh_list_artifact_files"])
    assert artifact_files_output["workflow_head_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert artifact_files_output["archive_sha256"]["pattern"] == r"^[0-9a-f]{64}$"
    assert artifact_files_output["truncated"]["type"] == "boolean"

    artifact_read_schema = tools["gh_read_artifact_file"].input_schema["properties"]
    assert artifact_read_schema["artifact_id"]["minimum"] == 1
    assert artifact_read_schema["path"]["maxLength"] == 4096
    assert artifact_read_schema["max_bytes"]["anyOf"][0]["maximum"] == 1_000_000
    artifact_read_output = _output_properties(tools["gh_read_artifact_file"])
    assert artifact_read_output["workflow_head_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert artifact_read_output["archive_sha256"]["pattern"] == r"^[0-9a-f]{64}$"
    assert artifact_read_output["sha256"]["pattern"] == r"^[0-9a-f]{64}$"
    assert artifact_read_output["truncated"]["type"] == "boolean"

    for name in ("gh_get_job_logs", "gh_get_run_logs"):
        schema = tools[name].input_schema["properties"]
        assert schema["attempt"]["minimum"] == 1
        assert schema["max_bytes"]["anyOf"][0]["maximum"] == 1_000_000
        assert schema["tail_bytes"]["anyOf"][0]["maximum"] == 1_000_000
        assert schema["start_marker"]["anyOf"][0]["minLength"] == 1
        output = _output_properties(tools[name])
        assert output["text"]["type"] == "string"
        assert output["truncated"]["type"] == "boolean"
        assert output["sha256"]["pattern"] == r"^[0-9a-f]{64}$"
    assert tools["gh_get_job_logs"].input_schema["properties"]["job_id"]["minimum"] == 1
    assert tools["gh_get_run_logs"].input_schema["properties"]["run_id"]["minimum"] == 1

    for name, description in EXPECTED_DESCRIPTIONS.items():
        assert tools[name].description == description
