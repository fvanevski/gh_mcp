"""Pull-request discovery, evidence, review, merge, and edit tools."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from pydantic import Field

from ..models import (
    PullRequestCheck,
    PullRequestChecks,
    PullRequestCommit,
    PullRequestCommitsPage,
    PullRequestCreate,
    PullRequestDiff,
    PullRequestEdit,
    PullRequestFile,
    PullRequestFilesPage,
    PullRequestInfo,
    PullRequestMerge,
    PullRequestReviewSubmission,
    SearchResults,
)
from ..request_governor import GitHubRequestError
from ..tooling import (
    ADD_EXTERNAL,
    MUTATE_EXTERNAL,
    OBJECT_SHA_RE,
    READ_EXTERNAL,
    AppContext,
    api_json_write,
    app_from_context,
    bounded_utf8,
    logger,
    mcp,
    optional_created_url,
    readback_warning,
    require_write_enabled,
    trailing_number,
    validate_repository,
)


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_list_prs(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
    state: str = "open",
    per_page: int | None = None,
) -> SearchResults:
    """List pull requests in a repository.

    state: open, closed, or all (default: open).
    """

    app = app_from_context(ctx)
    limit = app.client.clamp_max_results(per_page)
    fields = (
        "title,url,number,state,author,body,createdAt,updatedAt,closedAt,"
        "labels,comments,headRefName,baseRefName,isDraft,"
        "headRefOid,baseRefOid,additions,deletions,changedFiles"
    )
    result = await app.client.run(
        "pr",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--json",
        fields,
        "--state",
        state,
        "--limit",
        str(limit),
    )
    items: list[Any] = result if isinstance(result, list) else []
    return SearchResults(
        total_count=len(items),
        items=items,
        truncated=len(items) >= limit,
        query=f"{owner}/{repo} PRs ({state})",
    )


@mcp.tool(
    title="Get pull request snapshot",
    description=(
        "Read-only: return bounded metadata and exact base/head commit SHAs for one "
        "GitHub pull request. Performs one noninteractive GET request and cannot create "
        "comments, submit reviews, merge the pull request, request approval, or modify "
        "GitHub state."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_pr(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_.-]+$",
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    number: Annotated[int, Field(ge=1, description="Positive pull request number.")],
    *,
    ctx: Context[AppContext],
) -> PullRequestInfo:
    """Return a bounded, fully typed snapshot for one pull request."""

    logger.info("MCP tool invocation reached server: tool=gh_get_pr")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}",
        "-X",
        "GET",
    )
    if not isinstance(result, dict):
        raise RuntimeError("GitHub did not return pull-request metadata")

    base = result.get("base")
    head = result.get("head")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(base_sha, str) or not OBJECT_SHA_RE.fullmatch(base_sha):
        raise RuntimeError("GitHub did not return a valid pull-request base SHA")
    if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub did not return a valid pull-request head SHA")

    labels = result.get("labels")
    label_names = (
        [
            label["name"]
            for label in labels
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        ]
        if isinstance(labels, list)
        else []
    )
    issue_comments = result.get("comments")
    review_comments = result.get("review_comments")
    comment_count = (
        issue_comments if isinstance(issue_comments, int) and issue_comments >= 0 else 0
    ) + (review_comments if isinstance(review_comments, int) and review_comments >= 0 else 0)
    user = result.get("user")

    return PullRequestInfo(
        number=number,
        title=str(result.get("title") or ""),
        state=str(result.get("state") or "unknown"),
        body=str(result.get("body")) if result.get("body") is not None else None,
        author=user.get("login") if isinstance(user, dict) else None,
        createdAt=result.get("created_at"),
        updatedAt=result.get("updated_at"),
        closedAt=result.get("closed_at"),
        labels=label_names,
        comments=comment_count,
        url=str(result.get("html_url") or ""),
        headRefName=head.get("ref") if isinstance(head, dict) else None,
        baseRefName=base.get("ref") if isinstance(base, dict) else None,
        headRefOid=head_sha,
        baseRefOid=base_sha,
        isDraft=bool(result.get("draft", False)),
        additions=int(result.get("additions") or 0),
        deletions=int(result.get("deletions") or 0),
        changedFiles=int(result.get("changed_files") or 0),
    )


async def _get_pr_metadata(app: AppContext, owner: str, repo: str, number: int) -> dict[str, Any]:
    """Read one pull-request metadata object through an explicit GET."""

    metadata = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}",
        "-X",
        "GET",
    )
    if not isinstance(metadata, dict):
        raise RuntimeError("GitHub did not return pull-request metadata")
    return metadata


def _extract_pr_shas(metadata: dict[str, Any]) -> tuple[str, str]:
    """Validate immutable base and head object IDs from pull-request metadata."""

    base = metadata.get("base")
    head = metadata.get("head")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(base_sha, str) or not OBJECT_SHA_RE.fullmatch(base_sha):
        raise RuntimeError("GitHub did not return a valid pull-request base SHA")
    if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub did not return a valid pull-request head SHA")
    return base_sha, head_sha


async def _get_pr_shas(app: AppContext, owner: str, repo: str, number: int) -> tuple[str, str]:
    """Resolve and validate the immutable base and head object IDs for a PR."""

    return _extract_pr_shas(await _get_pr_metadata(app, owner, repo, number))


async def _verify_pr_shas(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
    expected: tuple[str, str],
) -> None:
    """Reject a numbered-PR read if its snapshot changed during the request."""

    if await _get_pr_shas(app, owner, repo, number) != expected:
        raise RuntimeError(
            "Pull request base or head changed during the read; retry from a fresh snapshot"
        )


@mcp.tool(
    title="Read pull request diff",
    description=(
        "Read-only: return a bounded unified diff or patch for the exact immutable base "
        "and head commit SHAs currently identified by a pull request. The result reports "
        "truncation, byte counts, and a SHA-256 fingerprint. This tool never checks out code, "
        "runs tests, requests approval, or modifies GitHub."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_pr_diff(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    *,
    ctx: Context[AppContext],
    format: Annotated[
        Literal["diff", "patch"],
        Field(description="Unified diff or email-style patch output."),
    ] = "diff",
    max_bytes: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000_000,
            description=(
                "Maximum UTF-8 bytes returned, capped by MCP_GH_MAX_PR_DIFF_BYTES. "
                "Omit to use the server cap."
            ),
        ),
    ] = None,
) -> PullRequestDiff:
    """Return a bounded diff for the immutable object IDs resolved from a PR."""

    logger.info("MCP tool invocation reached server: tool=gh_get_pr_diff")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    base_sha, head_sha = await _get_pr_shas(app, owner, repo, number)
    accept = (
        "application/vnd.github.v3.diff" if format == "diff" else "application/vnd.github.v3.patch"
    )
    response = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
        "-X",
        "GET",
        "-H",
        f"Accept: {accept}",
        json_output=False,
    )
    content = response.get("stdout") if isinstance(response, dict) else None
    if not isinstance(content, str):
        raise RuntimeError("GitHub did not return pull-request diff text")
    limit = min(max_bytes or app.settings.max_pr_diff_bytes, app.settings.max_pr_diff_bytes)
    bounded, returned, total, truncated, digest = bounded_utf8(content, limit)
    return PullRequestDiff(
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        format=format,
        content=bounded,
        truncated=truncated,
        bytes_returned=returned,
        total_bytes=total,
        sha256=digest,
    )


@mcp.tool(
    title="List pull request files",
    description=(
        "Read-only: return one bounded page of files changed by a pull request, together "
        "with its exact base and head SHAs. A file patch may be absent or truncated by "
        "GitHub; use gh_get_pr_diff for the bounded unified diff. This tool never modifies GitHub."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_list_pr_files(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    *,
    ctx: Context[AppContext],
    page: Annotated[int, Field(ge=1, le=10_000, description="One-based result page.")] = 1,
    per_page: Annotated[
        int | None,
        Field(ge=1, le=100, description="Results per page, capped by server policy."),
    ] = None,
) -> PullRequestFilesPage:
    """Return one explicitly bounded page of changed files."""

    logger.info("MCP tool invocation reached server: tool=gh_list_pr_files")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    limit = min(app.client.clamp_max_results(per_page), 100)
    base_sha, head_sha = await _get_pr_shas(app, owner, repo, number)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}/files",
        "-X",
        "GET",
        "-f",
        f"page={page}",
        "-f",
        f"per_page={limit}",
    )
    await _verify_pr_shas(app, owner, repo, number, (base_sha, head_sha))
    items = result if isinstance(result, list) else []
    files: list[PullRequestFile] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        patch = item.get("patch")
        patch_text = patch if isinstance(patch, str) else None
        patch_returned = 0
        patch_truncated = False
        if patch_text is not None:
            patch_text, patch_returned, _, patch_truncated, _ = bounded_utf8(
                patch_text, app.settings.max_pr_file_patch_bytes
            )
        files.append(
            PullRequestFile.model_validate(
                {
                    **item,
                    "patch": patch_text,
                    "patch_truncated": patch_truncated,
                    "patch_bytes_returned": patch_returned,
                }
            )
        )
    return PullRequestFilesPage(
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        page=page,
        per_page=limit,
        has_more=len(files) == limit,
        files=files,
    )


@mcp.tool(
    title="List pull request commits",
    description=(
        "Read-only: return one bounded page of commits in a pull request, together with "
        "its exact base and head SHAs. This tool never checks out code or modifies GitHub."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_list_pr_commits(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    *,
    ctx: Context[AppContext],
    page: Annotated[int, Field(ge=1, le=10_000, description="One-based result page.")] = 1,
    per_page: Annotated[
        int | None,
        Field(ge=1, le=100, description="Results per page, capped by server policy."),
    ] = None,
) -> PullRequestCommitsPage:
    """Return one explicitly bounded page of pull-request commits."""

    logger.info("MCP tool invocation reached server: tool=gh_list_pr_commits")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    limit = min(app.client.clamp_max_results(per_page), 100)
    base_sha, head_sha = await _get_pr_shas(app, owner, repo, number)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/pulls/{number}/commits",
        "-X",
        "GET",
        "-f",
        f"page={page}",
        "-f",
        f"per_page={limit}",
    )
    await _verify_pr_shas(app, owner, repo, number, (base_sha, head_sha))
    items = result if isinstance(result, list) else []
    commits: list[PullRequestCommit] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        git_commit = item.get("commit")
        git_commit = git_commit if isinstance(git_commit, dict) else {}
        author = git_commit.get("author")
        author = author if isinstance(author, dict) else {}
        committer = git_commit.get("committer")
        committer = committer if isinstance(committer, dict) else {}
        author_account = item.get("author")
        author_account = author_account if isinstance(author_account, dict) else {}
        committer_account = item.get("committer")
        committer_account = committer_account if isinstance(committer_account, dict) else {}
        message, message_returned, _, message_truncated, _ = bounded_utf8(
            str(git_commit.get("message", "")), app.settings.max_pr_commit_message_bytes
        )
        commits.append(
            PullRequestCommit(
                sha=str(item.get("sha", "")),
                message=message,
                message_truncated=message_truncated,
                message_bytes_returned=message_returned,
                author_login=author_account.get("login"),
                author_name=author.get("name"),
                authored_at=author.get("date"),
                committer_login=committer_account.get("login"),
                committed_at=committer.get("date"),
                url=str(item.get("html_url", "")),
            )
        )
    return PullRequestCommitsPage(
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        page=page,
        per_page=limit,
        has_more=len(commits) == limit,
        commits=commits,
    )


@mcp.tool(
    title="Get pull request checks",
    description=(
        "Read-only: return a bounded structured summary of CI checks for one exact "
        "pull-request head revision. This performs no watching, log download, workflow "
        "dispatch, approval, or GitHub write."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_pr_checks(
    owner: Annotated[
        str,
        Field(
            min_length=1,
            max_length=39,
            pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
            description="Canonical GitHub repository owner.",
        ),
    ],
    repo: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_.-]+$",
            description="Canonical GitHub repository name without path separators.",
        ),
    ],
    number: Annotated[int, Field(ge=1, description="Positive pull request number.")],
    *,
    ctx: Context[AppContext],
    required_only: Annotated[
        bool,
        Field(description="Return only checks required by branch protection."),
    ] = False,
    max_checks: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000,
            description="Maximum checks returned, capped by server result policy.",
        ),
    ] = None,
) -> PullRequestChecks:
    """Return a bounded check summary pinned to an unchanged PR revision."""

    logger.info("MCP tool invocation reached server: tool=gh_get_pr_checks")
    app = app_from_context(ctx)
    validate_repository(owner, repo)
    base_sha, head_sha = await _get_pr_shas(app, owner, repo, number)
    args = [
        "pr",
        "checks",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        "bucket,completedAt,description,event,link,name,startedAt,state,workflow",
    ]
    if required_only:
        args.append("--required")
    try:
        result = await app.client.run(*args, expected_returncode={0, 1, 8})
    except GitHubRequestError as exc:
        prefix = "gh command returned status 1 without structured output: "
        message = str(exc)
        detail = message.removeprefix(prefix) if message.startswith(prefix) else ""
        no_checks = detail.startswith("no checks reported on the '") and detail.endswith(
            "' branch"
        )
        no_required_checks = (
            required_only
            and detail.startswith("no required checks reported on the '")
            and detail.endswith("' branch")
        )
        if not (no_checks or no_required_checks):
            raise
        result = []
    if not isinstance(result, list):
        raise RuntimeError("GitHub CLI did not return structured pull-request checks")
    await _verify_pr_shas(app, owner, repo, number, (base_sha, head_sha))

    limit = min(max_checks or app.settings.hard_max_results, app.settings.hard_max_results, 1_000)
    checks = [
        PullRequestCheck(
            name=str(item.get("name", "")),
            state=str(item.get("state", "UNKNOWN")),
            bucket=item.get("bucket", "pending"),
            workflow=item.get("workflow"),
            event=item.get("event"),
            description=item.get("description"),
            started_at=item.get("startedAt"),
            completed_at=item.get("completedAt"),
            link=item.get("link"),
        )
        for item in result[:limit]
        if isinstance(item, dict)
    ]
    return PullRequestChecks(
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        total_count=len(result),
        truncated=len(result) > limit,
        checks=checks,
    )


@mcp.tool(
    title="Submit pull request review",
    description=(
        "Write action: submit a formal APPROVED, CHANGES_REQUESTED, or COMMENTED "
        "GitHub review for one pull request at an exact expected head commit. This is "
        "not an issue comment, never prompts, and never merges the pull request."
    ),
    annotations=ADD_EXTERNAL,
)
async def gh_submit_pr_review(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    expected_head_sha: Annotated[
        str,
        Field(
            pattern=r"^[0-9A-Fa-f]{40}$",
            description="Exact pull-request head SHA that was reviewed.",
        ),
    ],
    action: Annotated[
        Literal["approve", "request_changes", "comment"],
        Field(description="Formal GitHub review disposition."),
    ],
    *,
    ctx: Context[AppContext],
    body: Annotated[
        str,
        Field(
            max_length=65_536,
            description=(
                "Review body. Required for request_changes and comment; optional for approve."
            ),
        ),
    ] = "",
) -> PullRequestReviewSubmission:
    """Submit and read back a formal review pinned to an exact PR commit."""

    logger.info("MCP tool invocation reached server: tool=gh_submit_pr_review")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_review")
    if action in {"request_changes", "comment"} and not body.strip():
        raise ValueError(f"a non-empty review body is required for {action}")

    metadata = await _get_pr_metadata(app, owner, repo, number)
    _, current_head_sha = _extract_pr_shas(metadata)
    expected = expected_head_sha.lower()
    if current_head_sha.lower() != expected:
        raise RuntimeError(
            f"Pull request head changed: expected {expected}, current {current_head_sha}"
        )

    if action == "approve":
        viewer = await app.client.run("api", "user", "-X", "GET")
        viewer_login = viewer.get("login") if isinstance(viewer, dict) else None
        author = metadata.get("user")
        author_login = author.get("login") if isinstance(author, dict) else None
        if (
            isinstance(viewer_login, str)
            and isinstance(author_login, str)
            and viewer_login.casefold() == author_login.casefold()
        ):
            raise ValueError(
                f"authenticated GitHub account {viewer_login!r} is the pull request author "
                "and cannot approve its own pull request; no review was attempted"
            )

    event = {
        "approve": "APPROVE",
        "request_changes": "REQUEST_CHANGES",
        "comment": "COMMENT",
    }[action]
    created = await api_json_write(
        app.client,
        "POST",
        f"repos/{owner}/{repo}/pulls/{number}/reviews",
        {"body": body, "event": event, "commit_id": expected},
    )
    review_id = created.get("id")
    review_url = str(created.get("html_url", f"https://github.com/{owner}/{repo}/pull/{number}"))
    if not isinstance(review_id, int):
        warning = readback_warning("Pull request review", review_url)
        return PullRequestReviewSubmission(
            number=number,
            review_id=0,
            action=action,
            state=str(created.get("state", event)),
            body=body,
            commit_sha=expected,
            url=review_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    try:
        review = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}",
            "-X",
            "GET",
        )
        if not isinstance(review, dict):
            raise RuntimeError("GitHub returned a non-object review readback")
    except RuntimeError:
        warning = readback_warning("Pull request review", review_url)
        return PullRequestReviewSubmission(
            number=number,
            review_id=review_id,
            action=action,
            state=str(created.get("state", event)),
            body=str(created.get("body", body)),
            commit_sha=str(created.get("commit_id", expected)),
            url=review_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    user = review.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    return PullRequestReviewSubmission(
        number=number,
        review_id=review_id,
        action=action,
        state=str(review.get("state", event)),
        body=str(review.get("body", body)),
        author=author,
        submitted_at=review.get("submitted_at"),
        commit_sha=str(review.get("commit_id", expected)),
        url=str(review.get("html_url", review_url)),
        message=f"Formal pull request review submitted with state {review.get('state', event)}.",
    )


@mcp.tool(
    title="Merge pull request at exact head",
    description=(
        "Destructive write: merge one pull request using an explicit strategy only when "
        "its head still matches expected_head_sha. This tool cannot use administrator "
        "bypass, delete the branch, or silently merge a changed revision. It requires "
        "MCP_GH_ALLOW_PR_MERGE=true in addition to ordinary write authorization."
    ),
    annotations=MUTATE_EXTERNAL,
)
async def gh_merge_pr(
    owner: Annotated[str, Field(min_length=1, description="GitHub repository owner.")],
    repo: Annotated[str, Field(min_length=1, description="GitHub repository name.")],
    number: Annotated[int, Field(ge=1, description="Pull request number.")],
    expected_head_sha: Annotated[
        str,
        Field(
            pattern=r"^[0-9A-Fa-f]{40}$",
            description="Exact pull-request head SHA authorized for merge.",
        ),
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
    body: Annotated[
        str,
        Field(max_length=65_536, description="Optional merge commit body."),
    ] = "",
) -> PullRequestMerge:
    """Merge a PR with GitHub's atomic expected-head guard, then read it back."""

    logger.info("MCP tool invocation reached server: tool=gh_merge_pr")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_merge")
    _, current_head_sha = await _get_pr_shas(app, owner, repo, number)
    expected = expected_head_sha.lower()
    if current_head_sha.lower() != expected:
        raise RuntimeError(
            f"Pull request head changed: expected {expected}, current {current_head_sha}"
        )

    args = [
        "pr",
        "merge",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        f"--{method}",
        "--match-head-commit",
        expected,
        "--body-file",
        "-",
    ]
    if subject is not None:
        args.extend(["--subject", subject])
    await app.client.run(*args, json_output=False, stdin_text=body)

    pull_url = f"https://github.com/{owner}/{repo}/pull/{number}"
    try:
        result = await app.client.run(
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            ("number,url,state,mergedAt,mergeCommit,headRefOid,mergeStateStatus,autoMergeRequest"),
        )
    except RuntimeError:
        warning = readback_warning("Pull request merge", pull_url)
        return PullRequestMerge(
            number=number,
            method=method,
            head_sha=expected,
            state="UNKNOWN",
            merged=False,
            url=pull_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    merge_commit = result.get("mergeCommit")
    merge_commit_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    merged_at = result.get("mergedAt")
    state = str(result.get("state", "UNKNOWN"))
    merge_state_status = result.get("mergeStateStatus")
    merged = state.upper() == "MERGED" or isinstance(merged_at, str)
    queued = isinstance(merge_state_status, str) and merge_state_status.upper() == "QUEUED"
    auto_merge_enabled = isinstance(result.get("autoMergeRequest"), dict)
    if merged:
        message = "Pull request merged successfully."
    elif queued or auto_merge_enabled:
        message = "Merge command completed; the pull request is queued or awaiting requirements."
    else:
        message = f"Merge command completed; pull request state is {state}."
    return PullRequestMerge(
        number=number,
        method=method,
        head_sha=str(result.get("headRefOid", expected)),
        state=state,
        merged=merged,
        merge_queued=queued,
        auto_merge_enabled=auto_merge_enabled,
        merged_at=merged_at,
        merge_commit_sha=merge_commit_sha,
        merge_state_status=merge_state_status,
        url=str(result.get("url", pull_url)),
        message=message,
    )


@mcp.tool(annotations=ADD_EXTERNAL)
async def gh_create_pr(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
    *,
    ctx: Context[AppContext],
    draft: bool = False,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    review_users: list[str] | None = None,
) -> PullRequestCreate:
    """Create a new pull request in a repository.

    This tool is disabled unless MCP_GH_ALLOW_WRITE_COMMANDS=true. The MCP host
    is responsible for user-facing approval.
    """

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_create")
    args = [
        "pr",
        "create",
        "--repo",
        f"{owner}/{repo}",
        "--title",
        title,
        "--body-file",
        "-",
        "--head",
        head,
        "--base",
        base,
    ]
    if draft:
        args.append("--draft")
    if labels:
        for label in labels:
            args.extend(["--label", label])
    if assignees:
        for assignee in assignees:
            args.extend(["--assignee", assignee])
    if review_users:
        for user in review_users:
            args.extend(["--reviewer", user])

    create_result = await app.client.run(*args, json_output=False, stdin_text=body)
    created_url = optional_created_url(create_result)
    if created_url is None:
        warning = readback_warning("Pull request")
        return PullRequestCreate(
            number=0,
            title=title,
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    try:
        result = await app.client.run(
            "pr",
            "view",
            created_url,
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "title,number,url",
        )
    except RuntimeError:
        warning = readback_warning("Pull request", created_url)
        return PullRequestCreate(
            number=trailing_number(created_url),
            title=title,
            url=created_url,
            readback_completed=False,
            warning=warning,
            message=warning,
        )
    return PullRequestCreate(
        number=result.get("number", 0),
        title=result.get("title", title),
        url=result.get("url", ""),
        message="Pull request created successfully.",
    )


@mcp.tool(annotations=MUTATE_EXTERNAL)
async def gh_edit_pr(
    owner: str,
    repo: str,
    number: int,
    *,
    ctx: Context[AppContext],
    title: str | None = None,
    body: str | None = None,
    labels_add: list[str] | None = None,
    labels_remove: list[str] | None = None,
    assignees_add: list[str] | None = None,
    assignees_remove: list[str] | None = None,
    base: str | None = None,
) -> PullRequestEdit:
    """Edit an existing pull request."""

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="pr_edit")
    if not any(
        (
            title is not None,
            body is not None,
            labels_add,
            labels_remove,
            assignees_add,
            assignees_remove,
            base is not None,
        )
    ):
        raise ValueError("at least one pull request edit must be provided")
    if title == "":
        raise ValueError("pull request title cannot be empty")
    if base == "":
        raise ValueError("pull request base cannot be empty")

    args = [
        "pr",
        "edit",
        str(number),
        "--repo",
        f"{owner}/{repo}",
    ]
    if title is not None:
        args.extend(["--title", title])
    if body is not None:
        args.extend(["--body-file", "-"])
    if labels_add:
        for label in labels_add:
            args.extend(["--add-label", label])
    if labels_remove:
        for label in labels_remove:
            args.extend(["--remove-label", label])
    if assignees_add:
        for assignee in assignees_add:
            args.extend(["--add-assignee", assignee])
    if assignees_remove:
        for assignee in assignees_remove:
            args.extend(["--remove-assignee", assignee])
    if base is not None:
        args.extend(["--base", base])

    await app.client.run(*args, json_output=False, stdin_text=body if body is not None else None)
    fields = "title,url"
    try:
        info_result = await app.client.run(
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            fields,
        )
    except RuntimeError:
        warning = readback_warning("Pull request edit", f"{owner}/{repo}#{number}")
        return PullRequestEdit(
            number=number,
            title=title or "",
            url="",
            readback_completed=False,
            warning=warning,
            message=warning,
        )

    return PullRequestEdit(
        number=number,
        title=info_result.get("title", ""),
        url=info_result.get("url", ""),
        message="Pull request updated successfully.",
    )
