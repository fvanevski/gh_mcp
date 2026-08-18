"""Immutable historical release record for version 0.8.1."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_SURFACE = "Version 0.8.1 exposes 58 public MCP tools: 40 read-only and 18 write."
RELEASE_WRITE_TOOLS = {
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


def test_release_document_preserves_historical_surface() -> None:
    """Keep the shipped 0.8.1 record immutable as current runtime authority advances."""

    gate = (ROOT / "docs" / "release_gate_0_8_1.md").read_text()

    assert HISTORICAL_SURFACE in gate
    for tool_name in RELEASE_WRITE_TOOLS:
        assert tool_name in gate
    for retired in RETIRED_PUBLIC_TOOLS:
        assert retired in gate
    assert "host interception" in gate.casefold()
    assert "Do not retry" in gate
