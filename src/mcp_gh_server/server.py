"""MCP 2.0 composition root for GitHub CLI tool domains."""

from __future__ import annotations

from .legacy_action_write_adapter import gh_run_workflow
from .legacy_git_write_adapter import gh_create_branch, gh_create_branch_from_sha
from .legacy_issue_write_adapters import (
    gh_create_comment,
    gh_create_issue,
    gh_create_label,
    gh_create_milestone,
    gh_edit_issue,
    gh_edit_label,
    gh_upsert_label,
)
from .legacy_pr_write_adapters import gh_create_pr, gh_edit_pr, gh_merge_pr, gh_submit_pr_review
from .legacy_release_write_adapter import gh_create_release
from .legacy_repository_write_adapters import gh_commit_files, gh_create_repo
from .tooling import (
    ADD_EXTERNAL,
    MUTATE_EXTERNAL,
    READ_EXTERNAL,
    AppContext,
    app_lifespan,
    mcp,
)
from .tools.actions import (
    gh_get_failed_run_logs,
    gh_get_run,
    gh_get_workflow,
    gh_list_run_jobs,
    gh_list_runs,
    gh_list_workflows,
    gh_watch_run,
)
from .tools.diagnostics import gh_info, gh_server_info
from .tools.discovery import gh_search_code, gh_search_issues, gh_search_repos
from .tools.git import gh_create_branch as _registered_gh_create_branch
from .tools.issues import (
    gh_get_issue,
    gh_list_issues,
    gh_list_labels,
    gh_list_milestones,
)
from .tools.pull_requests import (
    gh_get_pr,
    gh_get_pr_checks,
    gh_get_pr_diff,
    gh_list_pr_commits,
    gh_list_pr_files,
    gh_list_prs,
)
from .tools.releases import gh_get_release, gh_list_releases
from .tools.repositories import gh_get_file_contents, gh_get_repo, gh_list_repos

# Importing one function from tools.git registers that domain's original tools;
# every public write below is then rebound to the shared compatibility contract.
del _registered_gh_create_branch

# Preserve the four descriptions historically derived from longer server.py
# docstrings while replacing their implementations.
mcp.remove_tool("gh_create_issue")
mcp.add_tool(
    gh_create_issue,
    description=(
        "Create a new issue in a repository.\n\n"
        "This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host\n"
        "is responsible for user-facing approval."
    ),
    annotations=ADD_EXTERNAL,
)
mcp.remove_tool("gh_edit_issue")
mcp.add_tool(
    gh_edit_issue,
    description=(
        "Edit an existing issue in a repository.\n\n"
        "This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host\n"
        "is responsible for user-facing approval."
    ),
    annotations=MUTATE_EXTERNAL,
)
mcp.remove_tool("gh_list_milestones")
mcp.add_tool(
    gh_list_milestones,
    description=(
        "List milestones in a repository via the GitHub API.\n\n"
        "state: open, closed, or all (default: all)."
    ),
    annotations=READ_EXTERNAL,
)
mcp.remove_tool("gh_create_milestone")
mcp.add_tool(
    gh_create_milestone,
    description=(
        "Create a new milestone in a repository via the GitHub API.\n\n"
        "due_on: due date in ISO format (e.g. '2026-12-31').\n"
        "state: open or closed (default: open).\n\n"
        "This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host\n"
        "is responsible for user-facing approval."
    ),
    annotations=ADD_EXTERNAL,
)

# Issue/label/comment writes.
mcp.remove_tool("gh_create_label")
mcp.add_tool(gh_create_label, annotations=ADD_EXTERNAL)
mcp.remove_tool("gh_upsert_label")
mcp.add_tool(gh_upsert_label, annotations=MUTATE_EXTERNAL)
mcp.remove_tool("gh_edit_label")
mcp.add_tool(gh_edit_label, annotations=MUTATE_EXTERNAL)
mcp.remove_tool("gh_create_comment")
mcp.add_tool(gh_create_comment, annotations=ADD_EXTERNAL)

# Pull-request writes.
mcp.remove_tool("gh_create_pr")
mcp.add_tool(gh_create_pr, annotations=ADD_EXTERNAL)
mcp.remove_tool("gh_edit_pr")
mcp.add_tool(gh_edit_pr, annotations=MUTATE_EXTERNAL)
mcp.remove_tool("gh_submit_pr_review")
mcp.add_tool(
    gh_submit_pr_review,
    title="Submit pull request review",
    description=(
        "Write action: submit a formal APPROVED, CHANGES_REQUESTED, or COMMENTED "
        "GitHub review for one pull request at an exact expected head commit. This is "
        "not an issue comment, never prompts, and never merges the pull request."
    ),
    annotations=ADD_EXTERNAL,
)
mcp.remove_tool("gh_merge_pr")
mcp.add_tool(
    gh_merge_pr,
    title="Merge pull request at exact head",
    description=(
        "Destructive write: merge one pull request using an explicit strategy only when "
        "its head still matches expected_head_sha. This tool cannot use administrator "
        "bypass, delete the branch, or silently merge a changed revision. It requires "
        "MCP_GH_ALLOW_PR_MERGE=true in addition to ordinary write authorization."
    ),
    annotations=MUTATE_EXTERNAL,
)

# Repository/release/workflow writes.
mcp.remove_tool("gh_create_repo")
mcp.add_tool(gh_create_repo, annotations=ADD_EXTERNAL)
mcp.remove_tool("gh_commit_files")
mcp.add_tool(
    gh_commit_files,
    title="Commit repository files atomically",
    description=(
        "Write action: create or replace complete UTF-8 files in one Git commit and "
        "conditionally advance one branch only when its head matches expected_head_sha. "
        "This tool requires host approval and server-side content-commit authorization."
    ),
    annotations=MUTATE_EXTERNAL,
)
mcp.remove_tool("gh_create_release")
mcp.add_tool(gh_create_release, annotations=ADD_EXTERNAL)
mcp.remove_tool("gh_run_workflow")
mcp.add_tool(gh_run_workflow, annotations=MUTATE_EXTERNAL)

# Git-reference writes.
mcp.remove_tool("gh_create_branch")
mcp.add_tool(
    gh_create_branch,
    title="Create issue development branch",
    description=(
        "Additive write: create an issue development branch using a branch-name base. "
        "The base parameter does not accept a commit SHA; use gh_create_branch_from_sha "
        "when the branch must start at an immutable commit."
    ),
    annotations=ADD_EXTERNAL,
)
mcp.remove_tool("gh_create_branch_from_sha")
mcp.add_tool(
    gh_create_branch_from_sha,
    title="Create branch from exact commit",
    description=(
        "Additive write: create one new branch at an exact 40-character commit SHA. "
        "The operation never moves or overwrites an existing branch, does not associate "
        "the branch with an issue, and performs no interactive prompting or MCP elicitation."
    ),
    annotations=ADD_EXTERNAL,
)

__all__ = [
    "AppContext",
    "app_lifespan",
    "gh_commit_files",
    "gh_create_branch",
    "gh_create_branch_from_sha",
    "gh_create_comment",
    "gh_create_issue",
    "gh_create_label",
    "gh_create_milestone",
    "gh_create_pr",
    "gh_create_release",
    "gh_create_repo",
    "gh_edit_issue",
    "gh_edit_label",
    "gh_edit_pr",
    "gh_get_failed_run_logs",
    "gh_get_file_contents",
    "gh_get_issue",
    "gh_get_pr",
    "gh_get_pr_checks",
    "gh_get_pr_diff",
    "gh_get_release",
    "gh_get_repo",
    "gh_get_run",
    "gh_get_workflow",
    "gh_info",
    "gh_list_issues",
    "gh_list_labels",
    "gh_list_milestones",
    "gh_list_pr_commits",
    "gh_list_pr_files",
    "gh_list_prs",
    "gh_list_releases",
    "gh_list_repos",
    "gh_list_run_jobs",
    "gh_list_runs",
    "gh_list_workflows",
    "gh_merge_pr",
    "gh_run_workflow",
    "gh_search_code",
    "gh_search_issues",
    "gh_search_repos",
    "gh_server_info",
    "gh_submit_pr_review",
    "gh_upsert_label",
    "gh_watch_run",
    "mcp",
]
