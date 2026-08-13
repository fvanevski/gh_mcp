"""Issue-scoped test configuration for high-risk write target policy."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def configure_existing_high_risk_write_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supply new target prerequisites to tests whose subject is downstream behavior."""

    module_name = request.module.__name__
    if module_name.endswith("test_write_wrappers"):
        monkeypatch.setenv("MCP_GH_ALLOWED_REPO_CREATION_TARGETS", "octo/new-repo")
        monkeypatch.setenv("MCP_GH_ALLOWED_WORKFLOW_DISPATCH_TARGETS", "octo/repo@99")
    elif module_name.endswith(
        ("test_workflow_dispatch_exact", "test_workflow_dispatch_exact_cancellation")
    ):
        monkeypatch.setenv("MCP_GH_ALLOWED_WORKFLOW_DISPATCH_TARGETS", "octo/repo@17")
