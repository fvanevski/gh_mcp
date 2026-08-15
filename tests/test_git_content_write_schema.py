"""Public schema regressions for issue #60 Git/content write migration."""

from __future__ import annotations

from mcp_gh_server.server import mcp


async def test_migrated_public_outputs_expose_shared_exact_outcome_fields() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    exact_outcome_fields = {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
    }

    for name in ("gh_create_branch", "gh_create_branch_from_sha", "gh_commit_files"):
        assert exact_outcome_fields <= set(tools[name].output_schema["properties"])


async def test_commit_files_output_exposes_reconciliation_evidence() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    properties = tools["gh_commit_files"].output_schema["properties"]

    assert {"observed_head_sha", "readback_attempts"} <= set(properties)
    assert {entry["type"] for entry in properties["ref_updated"]["anyOf"]} == {
        "boolean",
        "null",
    }
