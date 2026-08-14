"""Immutable historical release record for version 0.7.0."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_SURFACE = "Version 0.7.0 exposes 56 public MCP tools: 35 read-only and 21 write."
RELEASE_NEW_TOOLS = {
    "gh_get_ref",
    "gh_get_commit",
    "gh_list_run_artifacts",
    "gh_get_artifact",
    "gh_get_job_logs",
    "gh_get_run_logs",
    "gh_run_workflow_exact",
    "gh_create_release_exact",
    "gh_set_issue_state",
    "gh_list_pr_reviews",
    "gh_get_pr_review_state",
    "gh_set_pr_draft_state",
}


def test_release_document_preserves_historical_surface() -> None:
    """Keep the shipped 0.7.0 record immutable as current runtime authority advances."""

    gate = (ROOT / "docs" / "release_gate_0_7_0.md").read_text()

    assert HISTORICAL_SURFACE in gate
    for tool_name in RELEASE_NEW_TOOLS:
        assert tool_name in gate
    for phrase in (
        "arbitrary public `gh <args...>`",
        "arbitrary public `gh api`",
        "generic shell or subprocess MCP tool",
        "administrator bypasses",
        "automatic repeated workflow rerun or dispatch",
        "artifact or log deletion",
        "branch-protection or ruleset mutation",
    ):
        assert phrase in gate
