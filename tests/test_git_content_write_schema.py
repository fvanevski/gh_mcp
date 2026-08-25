"Public schema regressions for exact Git/content write tools."

from __future__ import annotations

from typing import Any

from mcp_gh_server.server import mcp


def _output_properties(tool: Any) -> dict[str, Any]:
    output_schema = tool.output_schema
    assert output_schema is not None
    properties = output_schema.get("properties")
    assert isinstance(properties, dict)
    return properties


async def test_public_git_content_outputs_expose_shared_exact_outcome_fields() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    exact_outcome_fields = {
        "precondition_checked",
        "write_completed",
        "readback_completed",
        "state_matches_requested",
        "warning",
        "request_id",
    }

    for name in (
        "gh_create_branch",
        "gh_create_branch_from_sha",
        "gh_commit_files",
        "gh_patch_files",
    ):
        assert exact_outcome_fields <= set(_output_properties(tools[name]))


async def test_content_write_outputs_expose_reconciliation_evidence() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    for name in ("gh_commit_files", "gh_patch_files"):
        properties = _output_properties(tools[name])
        assert {"observed_head_sha", "readback_attempts"} <= set(properties)
        assert {entry["type"] for entry in properties["ref_updated"]["anyOf"]} == {
            "boolean",
            "null",
        }


async def test_patch_files_schema_is_exact_context_only_and_commit_files_is_unchanged() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    patch_input = tools["gh_patch_files"].input_schema
    commit_input = tools["gh_commit_files"].input_schema

    assert set(patch_input["properties"]) == {
        "owner",
        "repo",
        "branch",
        "expected_head_sha",
        "patches",
        "commit_message",
    }
    assert "files" not in patch_input["properties"]
    assert set(commit_input["properties"]) == {
        "owner",
        "repo",
        "branch",
        "expected_head_sha",
        "files",
        "commit_message",
    }

    patch_output = _output_properties(tools["gh_patch_files"])
    assert {
        "changed_file_count",
        "applied_edit_count",
        "changed_paths",
        "files",
        "previous_head_sha",
        "commit_sha",
        "tree_sha",
    } <= set(patch_output)
