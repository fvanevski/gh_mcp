"""Host-facing action-specific formal pull-request review write schemas."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context
from pydantic import Field

from .pr_write_models import (
    PullRequestApproval,
    PullRequestChangesRequested,
    PullRequestCommentReview,
)
from .tooling import ADD_EXTERNAL, AppContext
from .tools.pr_review_writes import (
    gh_approve_pr as _gh_approve_pr,
)
from .tools.pr_review_writes import (
    gh_comment_pr_review as _gh_comment_pr_review,
)
from .tools.pr_review_writes import (
    gh_request_pr_changes as _gh_request_pr_changes,
)
from .write_tool_schema import (
    Body,
    ExactObjectSha,
    Owner,
    PositiveNumber,
    Repository,
    WriteToolMetadata,
)

ReviewerLogin = Annotated[
    str,
    Field(
        min_length=1,
        max_length=100,
        pattern=(
            r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})|"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,94})\[bot\])$"
        ),
        description=(
            "Exact reviewer actor login expected from the server-configured reviewer "
            "principal. This is a compare-only precondition and never selects credentials."
        ),
    ),
]


async def gh_approve_pr(
    owner: Owner,
    repo: Repository,
    number: Annotated[PositiveNumber, Field(description="Pull request number to approve.")],
    expected_head_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact pull-request head SHA that was independently reviewed."),
    ],
    expected_reviewer_login: ReviewerLogin,
    *,
    ctx: Context[AppContext],
    body: Annotated[
        Body,
        Field(description="Optional Markdown body for the APPROVED review."),
    ] = "",
) -> PullRequestApproval:
    result = await _gh_approve_pr(
        owner,
        repo,
        number,
        expected_head_sha,
        expected_reviewer_login,
        ctx=ctx,
        body=body,
    )
    return PullRequestApproval.model_validate(result.model_dump())


async def gh_request_pr_changes(
    owner: Owner,
    repo: Repository,
    number: Annotated[
        PositiveNumber,
        Field(description="Pull request number on which to request changes."),
    ],
    expected_head_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact pull-request head SHA that was independently reviewed."),
    ],
    expected_reviewer_login: ReviewerLogin,
    body: Annotated[
        Body,
        Field(description="Non-empty Markdown body explaining the requested changes."),
    ],
    *,
    ctx: Context[AppContext],
) -> PullRequestChangesRequested:
    result = await _gh_request_pr_changes(
        owner,
        repo,
        number,
        expected_head_sha,
        expected_reviewer_login,
        ctx=ctx,
        body=body,
    )
    return PullRequestChangesRequested.model_validate(result.model_dump())


async def gh_comment_pr_review(
    owner: Owner,
    repo: Repository,
    number: Annotated[
        PositiveNumber,
        Field(description="Pull request number on which to record a formal comment review."),
    ],
    expected_head_sha: Annotated[
        ExactObjectSha,
        Field(description="Exact pull-request head SHA to which the COMMENTED review is bound."),
    ],
    body: Annotated[
        Body,
        Field(
            description=(
                "Non-empty Markdown review body. This may record an external/Central "
                "positive disposition, but GitHub state remains COMMENTED, never APPROVED."
            )
        ),
    ],
    *,
    ctx: Context[AppContext],
) -> PullRequestCommentReview:
    result = await _gh_comment_pr_review(
        owner,
        repo,
        number,
        expected_head_sha,
        ctx=ctx,
        body=body,
    )
    return PullRequestCommentReview.model_validate(result.model_dump())


PR_REVIEW_WRITE_METADATA: dict[str, WriteToolMetadata] = {
    "gh_approve_pr": WriteToolMetadata(
        "Approve pull request at exact head",
        (
            "Additive write: submit exactly one formal GitHub APPROVED review for the "
            "supplied exact pull-request head through the server-configured independent "
            "reviewer principal. Before the review POST the server verifies repository write "
            "policy, current head, expected reviewer login, authenticated reviewer login, "
            "and reviewer != PR author. The caller cannot select credentials. The write is "
            "attempted once and immutable review-ID readback verifies APPROVED state, actor, "
            "head, and body. It never comments as a fallback, merges, dismisses reviews, or "
            "retries an ambiguous mutation automatically."
        ),
        ADD_EXTERNAL,
    ),
    "gh_request_pr_changes": WriteToolMetadata(
        "Request pull request changes at exact head",
        (
            "Additive write: submit exactly one formal GitHub CHANGES_REQUESTED review for "
            "the supplied exact pull-request head through the server-configured reviewer "
            "principal. The exact expected reviewer login is a compare-only precondition "
            "and cannot select credentials. The review POST is attempted once and immutable "
            "review-ID readback verifies state, actor, head, and body. It cannot approve, "
            "merge, dismiss reviews, or replay an ambiguous mutation."
        ),
        ADD_EXTERNAL,
    ),
    "gh_comment_pr_review": WriteToolMetadata(
        "Comment on pull request as formal review at exact head",
        (
            "Additive write: submit exactly one formal GitHub COMMENTED review through the "
            "ordinary authenticated GitHub principal for the supplied exact PR head. This "
            "is the explicit same-author fallback for recording an external or Central "
            "disposition; COMMENTED is never reported as GitHub APPROVED. The write is "
            "attempted once and immutable review-ID readback verifies actor, state, head, "
            "and body. It cannot select reviewer credentials, approve, merge, or retry an "
            "ambiguous mutation automatically."
        ),
        ADD_EXTERNAL,
    ),
}

PR_REVIEW_WRITE_TOOLS = (
    gh_approve_pr,
    gh_request_pr_changes,
    gh_comment_pr_review,
)
