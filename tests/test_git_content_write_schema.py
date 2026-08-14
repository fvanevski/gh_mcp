"""Public schema regressions for issue #60 Git/content write migration."""

from __future__ import annotations

import pytest

from mcp_gh_server.server import mcp


@pytest.mark.asyncio
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
