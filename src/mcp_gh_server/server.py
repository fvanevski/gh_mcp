"""MCP 2.0 composition root for GitHub CLI tool domains."""

from __future__ import annotations

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
    gh_run_workflow,
    gh_watch_run,
)
from .tools.diagnostics import gh_info, gh_server_info
from .tools.discovery import gh_search_code, gh_search_issues, gh_search_repos
from .tools.git import gh_create_branch, gh_create_branch_from_sha
from .tools.issues import (
    gh_create_comment,
    gh_create_issue,
    gh_create_label,
    gh_create_milestone,
    gh_edit_issue,
    gh_edit_label,
    gh_get_issue,
    gh_list_issues,
    gh_list_labels,
    gh_list_milestones,
    gh_upsert_label,
)
from .tools.pull_requests import (
    gh_create_pr,
    gh_edit_pr,
    gh_get_pr,
    gh_get_pr_checks,
    gh_get_pr_diff,
    gh_list_pr_commits,
    gh_list_pr_files,
    gh_list_prs,
    gh_merge_pr,
    gh_submit_pr_review,
)
from .tools.releases import gh_create_release, gh_get_release, gh_list_releases
from .tools.repositories import (
    gh_commit_files,
    gh_create_repo,
    gh_get_file_contents,
    gh_get_repo,
    gh_list_repos,
)

# These four tools historically derived their public descriptions from longer
# server.py docstrings. Preserve those descriptions explicitly at composition
# time while the implementation lives in the cohesive issue domain module.
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
