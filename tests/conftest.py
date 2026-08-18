"""Compatibility prerequisites for pre-issue-54 high-risk write regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

_WRITE_WRAPPER_REPO_TARGET_TESTS = {
    "test_create_repo_uses_visibility_and_readme_then_reads_repo",
}
_FORMAL_REVIEW_PROTOCOL_TEST = "test_streamable_http_formal_review_then_merge_without_nested_input"


@pytest.fixture(autouse=True)
def isolate_static_reviewer_protocol_test(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the static-reviewer protocol regression independent of deployment config."""

    if not (
        request.module.__name__.endswith("test_mcp_protocol")
        and request.node.name == _FORMAL_REVIEW_PROTOCOL_TEST
    ):
        return

    project = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MCP_GH_ENV_FILE", str(project / ".env.example"))
    for name in (
        "MCP_GH_REVIEWER_APP_ID",
        "MCP_GH_REVIEWER_INSTALLATION_ID",
        "MCP_GH_REVIEWER_PRIVATE_KEY_FILE",
        "MCP_GH_REVIEWER_LOGIN",
        "MCP_GH_REVIEWER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def configure_preexisting_high_risk_write_prerequisites(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supply only newly required target policy to exact pre-existing regressions.

    New issue-54 tests construct Settings explicitly and do not depend on this fixture.
    This shim is intentionally test-name scoped so unrelated tests retain their real
    default environment and cannot pass because an entire module received hidden write
    authorization.
    """

    module_name = request.module.__name__
    test_name = request.node.name

    if module_name.endswith("test_write_wrappers"):
        if test_name in _WRITE_WRAPPER_REPO_TARGET_TESTS:
            monkeypatch.setenv("MCP_GH_ALLOWED_REPO_CREATION_TARGETS", "octo/new-repo")
        return

    if module_name.endswith(
        ("test_workflow_dispatch_exact", "test_workflow_dispatch_exact_cancellation")
    ):
        monkeypatch.setenv("MCP_GH_ALLOWED_WORKFLOW_DISPATCH_TARGETS", "octo/repo@17")
