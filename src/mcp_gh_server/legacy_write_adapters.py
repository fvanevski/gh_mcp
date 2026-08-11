"""Backward-compatible internal imports for issue #9 write adapters.

The public MCP tools are composed in :mod:`mcp_gh_server.server`. New code should
import the cohesive domain adapters directly.
"""

from .legacy_pr_write_adapters import gh_merge_pr, gh_submit_pr_review
from .legacy_repository_write_adapters import gh_commit_files

__all__ = ["gh_commit_files", "gh_merge_pr", "gh_submit_pr_review"]
