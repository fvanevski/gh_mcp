"""Immutable historical release record for version 0.7.1."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_SURFACE = "Version 0.7.1 exposes 61 public MCP tools: 40 read-only and 21 write."
RELEASE_NEW_TOOLS = {
    "gh_get_merge_requirements",
    "gh_compare_commits",
    "gh_list_artifact_files",
    "gh_read_artifact_file",
    "gh_get_api_rate_status",
}


def test_release_document_preserves_historical_surface() -> None:
    """Keep the shipped 0.7.1 record immutable as current runtime authority advances."""

    gate = (ROOT / "docs" / "release_gate_0_7_1.md").read_text()

    assert HISTORICAL_SURFACE in gate
    for tool_name in RELEASE_NEW_TOOLS:
        assert tool_name in gate
    for phrase in (
        "arbitrary public `gh api`",
        "generic shell",
        "administrator",
        "automatic",
        "artifact deletion",
        "branch-protection",
    ):
        assert phrase in gate


def test_release_artifact_content_contract_is_documented() -> None:
    artifact_docs = (ROOT / "docs" / "gh_artifact_content.md").read_text()

    for phrase in (
        "exact `artifact_id`",
        "path traversal",
        "symbolic links",
        "strict UTF-8",
        "SHA-256",
        "Temporary ZIP state",
    ):
        assert phrase in artifact_docs
