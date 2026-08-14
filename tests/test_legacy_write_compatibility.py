"""Compatibility regressions for the frozen write-adapter facade during issue #59."""

from mcp_gh_server import legacy_write_adapters as legacy_writes
from mcp_gh_server.legacy_pr_merge_write_adapter import gh_merge_pr as legacy_merge_pr
from mcp_gh_server.legacy_pr_review_write_adapter import (
    gh_submit_pr_review as legacy_submit_pr_review,
)
from mcp_gh_server.legacy_repository_write_adapters import (
    gh_commit_files as legacy_commit_files,
)


def test_legacy_write_adapter_facade_remains_importable_until_issue_61() -> None:
    """Public PR routing may migrate without breaking the frozen compatibility facade."""

    assert legacy_writes.__all__ == ["gh_commit_files", "gh_merge_pr", "gh_submit_pr_review"]
    assert legacy_writes.gh_commit_files is legacy_commit_files
    assert legacy_writes.gh_merge_pr is legacy_merge_pr
    assert legacy_writes.gh_submit_pr_review is legacy_submit_pr_review
