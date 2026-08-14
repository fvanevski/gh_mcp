"""Canonical host-legible facade for the public write-tool surface.

This module owns public input schemas, titles, descriptions, and MCP annotations.
Each wrapper delegates execution unchanged to the existing write implementation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from mcp.server.mcpserver import Context
from mcp_types import ToolAnnotations
from pydantic import Field

from .issue_state_models import IssueState, IssueStateReason, IssueStateTransitionResult
from .issue_write_models import (
    IssueCreateResult,
    IssueEditResult,
    LabelCreateResult,
    LabelEditResult,
    MilestoneCreateResult,
)
from .legacy_git_write_adapter import (
    gh_create_branch as _gh_create_branch,
)
from .legacy_git_write_adapter import (
    gh_create_branch_from_sha as _gh_create_branch_from_sha,
)
from .legacy_pr_merge_write_adapter import gh_merge_pr as _gh_merge_pr
from .legacy_pr_metadata_write_adapter import (
    gh_create_pr as _gh_create_pr,
)
from .legacy_pr_metadata_write_adapter import (
    gh_edit_pr as _gh_edit_pr,
)
from .legacy_pr_review_write_adapter import gh_submit_pr_review as _gh_submit_pr_review
from .legacy_repository_write_adapters import (
    gh_commit_files as _gh_commit_files,
)
from .models import (
    BranchCreate,
    BranchCreateFromSha,
    CommentCreate,
    CommitFile,
    CommitFilesResult,
    PullRequestCreate,
    PullRequestEdit,
    PullRequestMerge,
    PullRequestReviewSubmission,
)
from .pr_draft_state_models import PullRequestDraftStateTransitionResult
from .release_exact_models import ReleaseExactResult
from .repository_create_models import RepositoryCreateResult
from .tooling import (
    ADD_EXTERNAL,
    MUTATE_EXTERNAL,
    OBJECT_SHA_RE,
    OWNER_RE,
    REPO_RE,
    AppContext,
)
from .tools.issue_state import gh_set_issue_state as _gh_set_issue_state
from .tools.issue_writes import (
    gh_create_issue as _gh_create_issue,
    gh_create_label as _gh_create_label,
    gh_create_milestone as _gh_create_milestone,
    gh_edit_issue as _gh_edit_issue,
    gh_edit_label as _gh_edit_label,
)
from .tools.issues import gh_create_comment as _gh_create_comment
from .tools.pr_draft_state import gh_set_pr_draft_state as _gh_set_pr_draft_state
from .tools.release_exact import gh_create_release_exact as _gh_create_release_exact
from .tools.repository_create import gh_create_repo as _gh_create_repo
from .tools.workflow_dispatch import gh_run_workflow_exact as _gh_run_workflow_exact
from .workflow_dispatch_models import WorkflowDispatchExactResult
from .workflow_selector import WORKFLOW_PATH_RE

Owner = Annotated[
    str,
    Field(
        description="GitHub repository owner or organization login.",
        min_length=1,
        max_length=39,
        pattern=OWNER_RE.pattern,
    ),
]
Repository = Annotated[
    str,
    Field(
        description="GitHub repository name without the owner prefix.",
        min_length=1,
        max_length=100,
        pattern=REPO_RE.pattern,
    ),
]
PositiveNumber = Annotated[
    int,
    Field(description="Positive GitHub object number.", ge=1),
]
ExactObjectSha = Annotated[
    str,
    Field(
        description="Exact 40-character hexadecimal Git object SHA.",
        pattern=OBJECT_SHA_RE.pattern,
    ),
]
Title = Annotated[str, Field(min_length=1, max_length=256)]
Body = Annotated[str, Field(max_length=65_536)]
Description = Annotated[str, Field(max_length=65_536)]
LabelName = Annotated[str, Field(min_length=1, max_length=100)]
LabelColor = Annotated[str, Field(pattern=r"^[0-9A-Fa-f]{6}$")]
Login = Annotated[
    str,
    Field(min_length=1, max_length=39, pattern=OWNER_RE.pattern),
]
AssigneeSelector = Annotated[
    str,
    Field(
        min_length=1,
        max_length=39,
        pattern=r"^(?:@me|[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))$",
    ),
]
Labels = Annotated[list[LabelName], Field(max_length=100)]
Assignees = Annotated[list[AssigneeSelector], Field(max_length=10)]
Reviewers = Annotated[list[Login], Field(max_length=100)]
BranchName = Annotated[str, Field(min_length=1, max_length=1024)]
TagName = Annotated[str, Field(min_length=1, max_length=1019)]
RefName = Annotated[
    str,
    Field(
        min_length=6,
        max_length=1024,
        pattern=r"^(?:heads|tags)/.+$",
    ),
]
WorkflowPath = Annotated[
    str,
    Field(
        min_length=23,
        max_length=1024,
        pattern=WORKFLOW_PATH_RE.pattern,
    ),
]
WorkflowInputKey = Annotated[str, Field(min_length=1, max_length=65_535)]
WorkflowInputValue = Annotated[str, Field(max_length=65_535)]
WorkflowInputs = Annotated[
    dict[WorkflowInputKey, WorkflowInputValue],
    Field(max_length=25),
]
DueOn = Annotated[str, Field(min_length=1, max_length=64)]
CommitMessage = Annotated[str, Field(min_length=1, max_length=65_536)]
ReleaseName = Annotated[str, Field(max_length=256)]


class PublicCommitFile(CommitFile):
    """Host-bounded file replacement payload for the public commit tool."""

    content: str = Field(
        description="Complete UTF-8 contents for the file.",
        max_length=5_000_000,
    )


PublicCommitFiles = Annotated[
    list[PublicCommitFile],
    Field(min_length=1, max_length=1000),
]


async def gh_create_issue(
    owner: Owner,
    repo: Repository,
    title: Annotated[Title, Field(description="Issue title.")],
    body: Annotated[
        Body | None,
        Field(description="Optional Markdown issue body."),
    ] = None,
    labels: Annotated[
        Labels | None,
        Field(description="Optional labels to apply."),
    ] = None,
    assignees: Annotated[
        Assignees | None,
        Field(description="Optional GitHub user logins or the @me selector to assign."),
    ] = None,
    *,
    ctx: Context[AppContext],
) -> IssueCreateResult:
    return await _gh_create_issue(
        owner,
        repo,
        title,
        body,
        labels,
        assignees,
        ctx=ctx,
    )


async def gh_edit_issue(
    owner: Owner,
    repo: Repository,
    number: Annotated[PositiveNumber, Field(description="Issue number to edit.")],
    *,
    ctx: Context[AppContext],
    title: Annotated[
        Title | None,
        Field(description="Replacement issue title."),
    ] = None,
    body: Annotated[
        Body | None,
        Field(description="Replacement Markdown issue body."),
    ] = None,
    labels_add: Annotated[
        Labels | None,
        Field(description="Labels to add."),
    ] = None,
    labels_remove: Annotated[
        Labels | None,
        Field(description="Labels to remove."),
    ] = None,
    assignees_add: Annotated[
        Assignees | None,
        Field(description="Assignee logins or the @me selector to add."),
    ] = None,
    assignees_remove: Annotated[
        Assignees | None,
        Field(description="Assignee logins or the @me selector to remove."),
    ] = None,
    milestone: Annotated[
        PositiveNumber | None,
        Field(description="Milestone number to set."),
    ] = None,
    remove_milestone: bool = False,
) -> IssueEditResult:
    return await _gh_edit_issue(
        owner,
        repo,
        number,
        ctx=ctx,
        title=title,
        body=body,
        labels_add=labels_add,
        labels_remove=labels_remove,
        assignees_add=assignees_add,
        assignees_remove=assignees_remove,
        milestone=milestone,
        remove_milestone=remove_milestone,
    )


async def gh_set_issue_state(
    owner: Owner,
    repo: Repository,
    number: Annotated[PositiveNumber, Field(description="Issue number to transition.")],
    expected_state: Annotated[
        IssueState,
        Field(description="Exact current issue state required before mutation."),
    ],
    new_state: Annotated[
        IssueState,
        Field(description="Requested state after the transition."),
    ],
    state_reason: Annotated[
        IssueStateReason,
        Field(description="Reason compatible with the requested issue state."),
    ],
    *,
    ctx: Context[AppContext],
) -> IssueStateTransitionResult:
    return await _gh_set_issue_state(
        owner,
        repo,
        number,
        expected_state,
        new_state,
        state_reason,
        ctx=ctx,
    )


async def gh_create_label(
    owner: Owner,
    repo: Repository,
    name: Annotated[LabelName, Field(description="New label name.")],
    color: Annotated[
        LabelColor,
        Field(description="Six-character hexadecimal label color."),
    ],
    *,
    ctx: Context[AppContext],
    description: Annotated[
        Description | None,
        Field(description="Optional label description."),
    ] = None,
) -> LabelCreateResult:
    return await _gh_create_label(
        owner,
        repo,
        name,
        color,
        ctx=ctx,
        description=description,
    )


async def gh_edit_label(
    owner: Owner,
    repo: Repository,
    name: Annotated[LabelName, Field(description="Existing label name.")],
    *,
    ctx: Context[AppContext],
    new_name: Annotated[
        LabelName | None,
        Field(description="Replacement label name."),
    ] = None,
    color: Annotated[
        LabelColor | None,
        Field(description="Replacement six-character hexadecimal label color."),
    ] = None,
    description: Annotated[
        Description | None,
        Field(description="Replacement label description."),
    ] = None,
) -> LabelEditResult:
    return await _gh_edit_label(
        owner,
        repo,
        name,
        ctx=ctx,
        new_name=new_name,
        color=color,
        description=description,
    )


async def gh_create_milestone(
    owner: Owner,
    repo: Repository,
    title: Annotated[Title, Field(description="Milestone title.")],
    *,
    ctx: Context[AppContext],
    description: Annotated[
        Description | None,
        Field(description="Optional milestone description."),
    ] = None,
    due_on: Annotated[
        DueOn | None,
        Field(description="Optional ISO-8601 milestone due date/time."),
    ] = None,
    state: Annotated[
        Literal["open", "closed"],
        Field(description="Initial milestone state."),
    ] = "open",
) -> MilestoneCreateResult:
    return await _gh_create_milestone(
        owner,
        repo,
        title,
        ctx=ctx,
        description=description,
        due_on=due_on,
        state=state,
    )


async def gh_create_comment(
    owner: Owner,
    repo: Repository,
    issue_number: Annotated[
        PositiveNumber,
        Field(description="Issue or pull request number to comment on."),
    ],
    body: Annotated[Body, Field(description="Markdown comment body.")],
    *,
    ctx: Context[AppContext],
) -> CommentCreate:
    return await _gh_create_comment(
        owner,
        repo,
        issue_number,
        body,
        ctx=ctx,
    )


async def gh_create_pr(
    owner: Owner,
    repo: Repository,
    title: Annotated[Title, Field(description="Pull request title.")],
    body: Annotated[Body, Field(description="Markdown pull request body.")],
    head: Annotated[
        BranchName,
        Field(description="Head branch or owner:branch selector."),
    ],
    base: Annotated[BranchName, Field(description="Base branch name.")],
    *,
    ctx: Context[AppContext],
    draft: bool = False,
    labels: Annotated[
        Labels | None,
        Field(description="Optional labels to apply."),
    ] = None,
    assignees: Annotated[
        Assignees | None,
        Field(description="Optional GitHub user logins or the @me selector to assign."),
    ] = None,
    review_users: Annotated[
        Reviewers | None,
        Field(description="Optional GitHub user logins to request for review."),
    ] = None,
) -> PullRequestCreate:
    return await _gh_create_pr(
        owner,
        repo,
        title,
        body,
        head,
        base,
        ctx=ctx,
        draft=draft,
        labels=labels,
        assignees=assignees,
        review_users=review_users,
    )


async def gh_edit_pr(
    owner: Owner,
    repo: Repository,
    number: Annotated[PositiveNumber, Field(description="Pull request number to edit.")],
    *,
    ctx: Context[AppContext],
    title: Annotated[
        Title | None,
        Field(description="Replacement pull request title."),
    ] = None,
    body: Annotated[
        Body | None,
        Field(description="Replacement Markdown pull request body."),
    ] = None,
    labels_add: Annotated[
        Labels | None,
        Field(description="Labels to add."),
    ] = None,
    labels_remove: Annotated[
        Labels | None,
        Field(description="Labels to remove."),
    ] = None,
    assignees_add: Annotated[
        Assignees | None,
        Field(description="Assignee logins or the @me selector to add."),
    ] = None,
    assignees_remove: Annotated[
        Assignees | None,
        Field(description="Assignee logins or the @me selector to remove."),
    ] = None,
    base: Annotated[
        BranchName | None,
        Field(description="Replacement base branch name."),
    ] = None,
) -> PullRequestEdit:
    return await _gh_edit_pr(
        owner,
        repo,
        number,
        ctx=ctx,
        title=title,
        body=body,
        labels_add=labels_add,
        labels_remove=labels_remove,
        assignees_add=assignees_add,
        assignees_remove=assignees_remove,
        base=base,
    )


async def gh_set_pr_draft_state(
    owner: Owner,
    repo: Repository,
    number: Annotated[
        PositiveNumber,
        Field(description="Pull request number to transition."),
    ],
    expected_head_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact current pull-request head SHA required before mutation."),
    ],
    expected_is_draft: Annotated[
        bool,
        Field(description="Exact current draft state required before mutation."),
    ],
    new_is_draft: Annotated[
        bool,
        Field(description="Requested draft state after mutation."),
    ],
    *,
    ctx: Context[AppContext],
) -> PullRequestDraftStateTransitionResult:
    return await _gh_set_pr_draft_state(
        owner,
        repo,
        number,
        expected_head_sha,
        expected_is_draft,
        new_is_draft,
        ctx=ctx,
    )


async def gh_submit_pr_review(
    owner: Owner,
    repo: Repository,
    number: Annotated[PositiveNumber, Field(description="Pull request number to review.")],
    expected_head_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact pull-request head SHA that was reviewed."),
    ],
    action: Annotated[
        Literal["approve", "request_changes", "comment"],
        Field(description="Formal GitHub review disposition."),
    ],
    *,
    ctx: Context[AppContext],
    body: Annotated[
        Body,
        Field(
            description=(
                "Review body; required for request_changes and comment and optional for approve."
            )
        ),
    ] = "",
) -> PullRequestReviewSubmission:
    return await _gh_submit_pr_review(
        owner,
        repo,
        number,
        expected_head_sha,
        action,
        ctx=ctx,
        body=body,
    )


async def gh_merge_pr(
    owner: Owner,
    repo: Repository,
    number: Annotated[PositiveNumber, Field(description="Pull request number to merge.")],
    expected_head_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact pull-request head SHA authorized for merge."),
    ],
    method: Annotated[
        Literal["merge", "squash", "rebase"],
        Field(description="Repository-supported merge strategy."),
    ],
    *,
    ctx: Context[AppContext],
    subject: Annotated[
        str | None,
        Field(max_length=256, description="Optional merge commit subject."),
    ] = None,
    body: Annotated[Body, Field(description="Optional merge commit body.")] = "",
) -> PullRequestMerge:
    return await _gh_merge_pr(
        owner,
        repo,
        number,
        expected_head_sha,
        method,
        ctx=ctx,
        subject=subject,
        body=body,
    )


async def gh_create_repo(
    owner: Owner,
    repo: Repository,
    *,
    ctx: Context[AppContext],
    description: Annotated[
        Description | None,
        Field(description="Optional repository description."),
    ] = None,
    private: bool = False,
    auto_init: bool = False,
) -> RepositoryCreateResult:
    return await _gh_create_repo(
        owner,
        repo,
        ctx=ctx,
        description=description,
        private=private,
        auto_init=auto_init,
    )


async def gh_commit_files(
    owner: Owner,
    repo: Repository,
    branch: Annotated[
        BranchName,
        Field(description="Existing branch to advance conditionally."),
    ],
    expected_head_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact branch head SHA required before the write."),
    ],
    files: Annotated[
        PublicCommitFiles,
        Field(description="Complete UTF-8 file replacements for the atomic commit."),
    ],
    commit_message: Annotated[
        CommitMessage,
        Field(description="Git commit message."),
    ],
    *,
    ctx: Context[AppContext],
) -> CommitFilesResult:
    normalized_files = [CommitFile.model_validate(file.model_dump()) for file in files]
    return await _gh_commit_files(
        owner,
        repo,
        branch,
        expected_head_sha,
        normalized_files,
        commit_message,
        ctx=ctx,
    )


async def gh_create_release_exact(
    owner: Owner,
    repo: Repository,
    tag_name: Annotated[TagName, Field(description="Exact Git tag name to create.")],
    expected_target_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact target commit SHA for the release tag."),
    ],
    make_latest: Annotated[
        bool,
        Field(description="Explicit latest-release policy for the created release."),
    ],
    *,
    ctx: Context[AppContext],
    name: Annotated[
        ReleaseName | None,
        Field(description="Optional release display name."),
    ] = None,
    body: Annotated[
        Body | None,
        Field(description="Optional Markdown release notes."),
    ] = None,
    draft: bool = False,
    prerelease: bool = False,
    expected_tag_absent: bool = True,
    expected_release_absent: bool = True,
) -> ReleaseExactResult:
    return await _gh_create_release_exact(
        owner,
        repo,
        tag_name,
        expected_target_sha,
        make_latest,
        ctx=ctx,
        name=name,
        body=body,
        draft=draft,
        prerelease=prerelease,
        expected_tag_absent=expected_tag_absent,
        expected_release_absent=expected_release_absent,
    )


async def gh_run_workflow_exact(
    owner: Owner,
    repo: Repository,
    workflow_id: Annotated[
        PositiveNumber,
        Field(description="Exact positive GitHub workflow ID to dispatch."),
    ],
    expected_workflow_path: Annotated[
        WorkflowPath,
        Field(
            description=(
                "Exact canonical case-sensitive workflow path that the numeric workflow ID "
                "must identify immediately before dispatch."
            )
        ),
    ],
    ref: Annotated[
        RefName,
        Field(description=("Exact ref path relative to refs/, as heads/<branch> or tags/<tag>.")),
    ],
    expected_ref_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact commit SHA the ref must resolve to before dispatch."),
    ],
    *,
    ctx: Context[AppContext],
    inputs: Annotated[
        WorkflowInputs | None,
        Field(
            description=(
                "Optional workflow_dispatch input object with at most 25 string entries and "
                "at most 65,535 aggregate key/value characters."
            )
        ),
    ] = None,
) -> WorkflowDispatchExactResult:
    return await _gh_run_workflow_exact(
        owner,
        repo,
        workflow_id,
        expected_workflow_path,
        ref,
        expected_ref_sha,
        ctx=ctx,
        inputs=inputs,
    )


async def gh_create_branch(
    owner: Owner,
    repo: Repository,
    issue_number: Annotated[
        PositiveNumber,
        Field(description="Positive issue number used by GitHub issue develop."),
    ],
    name: Annotated[
        BranchName,
        Field(description="New development branch name."),
    ],
    *,
    ctx: Context[AppContext],
    base: Annotated[
        BranchName | None,
        Field(
            description=(
                "Existing branch-name base; full commit SHAs are rejected by the implementation."
            )
        ),
    ] = None,
) -> BranchCreate:
    return await _gh_create_branch(
        owner,
        repo,
        issue_number,
        name,
        ctx=ctx,
        base=base,
    )


async def gh_create_branch_from_sha(
    owner: Owner,
    repo: Repository,
    name: Annotated[BranchName, Field(description="New branch name.")],
    base_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact commit SHA at which to create the new branch."),
    ],
    *,
    ctx: Context[AppContext],
) -> BranchCreateFromSha:
    return await _gh_create_branch_from_sha(
        owner,
        repo,
        name,
        base_sha,
        ctx=ctx,
    )


@dataclass(frozen=True, slots=True)
class WriteToolMetadata:
    title: str
    description: str
    annotations: ToolAnnotations


WRITE_TOOL_METADATA: dict[str, WriteToolMetadata] = {
    "gh_create_issue": WriteToolMetadata(
        "Create issue",
        (
            "Additive write: create exactly one issue in the target repository. "
            "The ordinary write gate and repository policy must allow the target. "
            "Optional labels and assignees are bounded; one mutation attempt is followed "
            "by authoritative semantic readback when stable identity is available. The "
            "tool never retries an ambiguous mutation automatically and does not edit, "
            "close, comment on, or delete an existing issue."
        ),
        ADD_EXTERNAL,
    ),
    "gh_edit_issue": WriteToolMetadata(
        "Edit issue metadata",
        (
            "Destructive write: edit metadata on exactly one existing issue after "
            "ordinary write authorization. The request may change title, body, labels, "
            "assignees, or milestone; one mutation attempt is followed by authoritative "
            "semantic readback of the requested fields. Ambiguous mutations are never "
            "retried automatically. It does not close or reopen the issue, post comments, "
            "delete the issue, or bypass repository policy."
        ),
        MUTATE_EXTERNAL,
    ),
    "gh_set_issue_state": WriteToolMetadata(
        "Set issue state with exact precondition",
        (
            "Destructive write: close or reopen exactly one issue only when its current "
            "state matches expected_state. Pull requests are rejected. Closing requires "
            "completed, not_planned, or duplicate; reopening requires reopened. The "
            "mutation is attempted once, comments remain a separate tool, and "
            "authoritative readback verifies the final state and reason."
        ),
        MUTATE_EXTERNAL,
    ),
    "gh_create_label": WriteToolMetadata(
        "Create label",
        (
            "Additive write: create exactly one new repository label after ordinary "
            "write authorization. Name, color, and description are explicitly bounded; "
            "one mutation attempt is followed by authoritative semantic readback. The "
            "operation never overwrites an existing label or retries an ambiguous create "
            "automatically, and it does not edit issues or delete labels."
        ),
        ADD_EXTERNAL,
    ),
    "gh_edit_label": WriteToolMetadata(
        "Edit label",
        (
            "Destructive write: edit exactly one existing label's name, color, or "
            "description after ordinary write authorization. One mutation attempt is "
            "followed by authoritative semantic readback of the resulting label; an "
            "ambiguous edit is never retried automatically. It does not delete labels or "
            "mutate issue content."
        ),
        MUTATE_EXTERNAL,
    ),
    "gh_create_milestone": WriteToolMetadata(
        "Create milestone",
        (
            "Additive write: create exactly one repository milestone with bounded title, "
            "description, due date, and explicit open/closed state after ordinary write "
            "authorization. One mutation attempt is followed by authoritative readback "
            "of the stable milestone number and requested fields; ambiguous creation is "
            "never retried automatically. It does not assign issues to the milestone or "
            "edit existing milestones."
        ),
        ADD_EXTERNAL,
    ),
    "gh_create_comment": WriteToolMetadata(
        "Create issue or pull request comment",
        (
            "Additive write: post exactly one bounded Markdown conversation comment on "
            "the specified issue or pull request after ordinary write authorization. "
            "The mutation is attempted once through the issue-comments REST endpoint, "
            "and authoritative readback of the returned immutable comment ID verifies "
            "repository and issue identity plus the requested body. It is not a formal "
            "pull-request review and cannot merge."
        ),
        ADD_EXTERNAL,
    ),
    "gh_create_pr": WriteToolMetadata(
        "Create pull request",
        (
            "Additive write: create exactly one pull request from the specified bounded "
            "head and base selectors after ordinary write authorization. Optional labels, "
            "assignees, and review requests are bounded and read back when created. It "
            "does not approve, merge, or change another pull request."
        ),
        ADD_EXTERNAL,
    ),
    "gh_edit_pr": WriteToolMetadata(
        "Edit pull request metadata",
        (
            "Destructive write: edit metadata on exactly one pull request after ordinary "
            "write authorization. The request may change title, body, labels, assignees, "
            "or base and uses authoritative readback for requested fields. Draft-state "
            "transition, formal review, merge, branch deletion, and administrator bypass "
            "are separate or unavailable."
        ),
        MUTATE_EXTERNAL,
    ),
    "gh_set_pr_draft_state": WriteToolMetadata(
        "Set pull request draft state at exact head",
        (
            "Destructive write: transition exactly one pull request between draft and "
            "ready-for-review only when its current head SHA and draft state match the "
            "supplied preconditions. The operation changes no unrelated pull-request "
            "metadata, is attempted once, and authoritative readback verifies both "
            "unchanged head identity and the requested draft state."
        ),
        MUTATE_EXTERNAL,
    ),
    "gh_submit_pr_review": WriteToolMetadata(
        "Submit pull request review at exact head",
        (
            "Additive write: submit one formal APPROVED, CHANGES_REQUESTED, or COMMENTED "
            "GitHub review only for the supplied exact pull-request head SHA. The ordinary "
            "write gate applies and authoritative readback verifies the created review "
            "when identity is available. This is not an issue comment and never merges "
            "the pull request."
        ),
        ADD_EXTERNAL,
    ),
    "gh_merge_pr": WriteToolMetadata(
        "Merge pull request at exact head",
        (
            "Destructive write: merge exactly one pull request using the explicit merge "
            "strategy only while its head matches expected_head_sha. Ordinary write "
            "authorization and the separate PR-merge fine gate are required. The tool "
            "cannot use administrator bypass, delete the branch, force a changed "
            "revision, or blindly retry an ambiguous merge."
        ),
        MUTATE_EXTERNAL,
    ),
    "gh_create_repo": WriteToolMetadata(
        "Create repository",
        (
            "Additive write: create exactly one repository at the canonical OWNER/REPO target "
            "after ordinary write policy, exact prospective-repository target policy, and the "
            "separate repository-creation fine gate allow it. The mutation is attempted once, "
            "then exact authoritative readback verifies repository identity, visibility, "
            "description, and initialization when GitHub exposes that evidence. It never "
            "retries an ambiguous creation and cannot delete, rename, transfer, or otherwise "
            "administer an existing repository."
        ),
        ADD_EXTERNAL,
    ),
    "gh_commit_files": WriteToolMetadata(
        "Commit repository files atomically",
        (
            "Destructive write: create or replace bounded UTF-8 file contents in one Git "
            "commit and conditionally advance exactly one existing branch only when its "
            "head matches expected_head_sha. Ordinary write authorization and the "
            "content-commit fine gate are required. The operation cannot delete files, "
            "force-update the ref, or blindly retry an ambiguous branch update."
        ),
        MUTATE_EXTERNAL,
    ),
    "gh_create_release_exact": WriteToolMetadata(
        "Create release at exact target",
        (
            "Additive write: create one GitHub release using an exact 40-character target "
            "commit SHA after ordinary write authorization and the separate release-creation "
            "fine gate. The tool verifies target identity, optionally requires the tag and "
            "every release state including drafts to be absent, performs exactly one governed "
            "creation request, and verifies release, tag commit, and explicit latest state. "
            "It never retries an ambiguous release mutation automatically."
        ),
        ADD_EXTERNAL,
    ),
    "gh_run_workflow_exact": WriteToolMetadata(
        "Dispatch workflow at exact ref",
        (
            "Destructive write: after ordinary write authorization, exact workflow-target "
            "policy, and the separate workflow-dispatch fine gate, dispatch exactly one "
            "positive workflow ID only when GitHub immediately re-verifies the caller's exact "
            "canonical workflow path and active state. The tool also verifies the exact "
            "branch/tag ref against expected_ref_sha, rejects same-name branch/tag ambiguity "
            "and an existing workflow_dispatch run for the workflow/head, accepts only a "
            "bounded typed input object, requests return_run_details, and binds authoritative "
            "readback to the exact returned run ID. It never redispatches automatically."
        ),
        MUTATE_EXTERNAL,
    ),
    "gh_create_branch": WriteToolMetadata(
        "Create issue development branch",
        (
            "Additive write: create exactly one issue development branch using a bounded "
            "branch-name base after ordinary write authorization. The base parameter "
            "rejects full commit SHAs; use gh_create_branch_from_sha for an immutable "
            "base. This compatibility surface does not claim exact semantic readback "
            "identity and cannot move or delete refs."
        ),
        ADD_EXTERNAL,
    ),
    "gh_create_branch_from_sha": WriteToolMetadata(
        "Create branch from exact commit",
        (
            "Additive write: create exactly one new branch at an exact 40-character "
            "commit SHA after ordinary write authorization. A branch already at the "
            "requested SHA is a safe no-write result; a conflicting existing branch is "
            "left unchanged. The operation never force-updates, moves, overwrites, or "
            "deletes an existing ref."
        ),
        ADD_EXTERNAL,
    ),
}

PublicWriteTool = Callable[..., Awaitable[object]]
PUBLIC_WRITE_TOOLS: tuple[PublicWriteTool, ...] = (
    gh_create_issue,
    gh_edit_issue,
    gh_set_issue_state,
    gh_create_label,
    gh_edit_label,
    gh_create_milestone,
    gh_create_comment,
    gh_create_pr,
    gh_edit_pr,
    gh_set_pr_draft_state,
    gh_submit_pr_review,
    gh_merge_pr,
    gh_create_repo,
    gh_commit_files,
    gh_create_release_exact,
    gh_run_workflow_exact,
    gh_create_branch,
    gh_create_branch_from_sha,
)
