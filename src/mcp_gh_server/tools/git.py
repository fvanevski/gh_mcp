"""Focused Git reference and commit tools."""

from __future__ import annotations

from typing import Annotated, cast
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from ..gh_client import GhClient
from ..models import (
    GitCommitInfo,
    GitCommitInput,
    GitCommitPerson,
    GitCommitVerification,
    GitObjectType,
    GitRefInfo,
    GitRefInput,
)
from ..request_governor import GitHubRequestError
from ..tooling import (
    OBJECT_SHA_RE,
    OWNER_RE,
    READ_EXTERNAL,
    REPO_RE,
    AppContext,
    app_from_context,
    logger,
    mcp,
    validate_repository,
)

_MAX_TAG_PEEL_DEPTH = 16


def _parse_git_object(raw: object, *, resource: str) -> tuple[GitObjectType, str, str]:
    """Validate one GitHub Git-object descriptor without upgrading ambiguous evidence."""

    if not isinstance(raw, dict):
        raise RuntimeError(f"GitHub returned no Git object for {resource}")
    object_type = raw.get("type")
    object_sha = raw.get("sha")
    object_url = raw.get("url")
    if object_type not in {"commit", "tag", "tree", "blob"}:
        raise RuntimeError(f"GitHub returned an unsupported Git object type for {resource}")
    if not isinstance(object_sha, str) or not OBJECT_SHA_RE.fullmatch(object_sha):
        raise RuntimeError(f"GitHub returned no exact 40-character object SHA for {resource}")
    if not isinstance(object_url, str) or not object_url:
        raise RuntimeError(f"GitHub returned no object URL for {resource}")
    return cast(GitObjectType, object_type), object_sha.casefold(), object_url


def _parse_commit_person(raw: object, *, role: str) -> GitCommitPerson:
    """Validate raw immutable commit author/committer identity."""

    if not isinstance(raw, dict):
        raise RuntimeError(f"GitHub returned no commit {role} identity")
    name = raw.get("name")
    email = raw.get("email")
    date = raw.get("date")
    if not all(isinstance(value, str) for value in (name, email, date)):
        raise RuntimeError(f"GitHub returned incomplete commit {role} identity")
    return GitCommitPerson(name=cast(str, name), email=cast(str, email), date=cast(str, date))


def _parse_commit_verification(raw: object) -> GitCommitVerification:
    """Preserve GitHub verification fields without interpreting their evidentiary meaning."""

    if not isinstance(raw, dict):
        raise RuntimeError("GitHub returned no commit verification metadata")
    verified = raw.get("verified")
    reason = raw.get("reason")
    if not isinstance(verified, bool) or not isinstance(reason, str):
        raise RuntimeError("GitHub returned malformed commit verification metadata")

    optional_fields: dict[str, str | None] = {}
    for field_name in ("signature", "payload", "verified_at"):
        value = raw.get(field_name)
        if value is not None and not isinstance(value, str):
            raise RuntimeError(
                f"GitHub returned malformed commit verification field {field_name!r}"
            )
        optional_fields[field_name] = value

    return GitCommitVerification(
        verified=verified,
        reason=reason,
        signature=optional_fields["signature"],
        payload=optional_fields["payload"],
        verified_at=optional_fields["verified_at"],
    )


async def _peel_annotated_tag(
    client: GhClient,
    owner: str,
    repo: str,
    tag_sha: str,
) -> str | None:
    """Peel a bounded chain of annotated tag objects to a commit when one exists."""

    current_sha = tag_sha
    seen: set[str] = set()
    for _ in range(_MAX_TAG_PEEL_DEPTH):
        if current_sha in seen:
            raise RuntimeError("GitHub annotated-tag peel returned a cyclic object chain")
        seen.add(current_sha)
        payload = await client.run(
            "api",
            f"repos/{owner}/{repo}/git/tags/{current_sha}",
            "-X",
            "GET",
        )
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"GitHub returned an unexpected response while peeling tag object {current_sha}"
            )
        returned_sha = payload.get("sha")
        if (
            not isinstance(returned_sha, str)
            or not OBJECT_SHA_RE.fullmatch(returned_sha)
            or returned_sha.casefold() != current_sha
        ):
            raise RuntimeError(
                "GitHub annotated-tag read did not preserve the exact requested tag-object SHA"
            )
        target_type, target_sha, _ = _parse_git_object(
            payload.get("object"),
            resource=f"annotated tag object {current_sha}",
        )
        if target_type == "commit":
            return target_sha
        if target_type != "tag":
            return None
        current_sha = target_sha

    raise RuntimeError(
        f"GitHub annotated-tag peel exceeded the {_MAX_TAG_PEEL_DEPTH}-object safety bound"
    )


async def _confirm_contents_read_access(
    client: GhClient,
    owner: str,
    repo: str,
    *,
    resource_kind: str = "ref",
) -> None:
    """Prove Contents-read access before classifying an exact-object 404 as missing."""

    result = await client.run(
        "api",
        f"repos/{owner}/{repo}/branches",
        "-X",
        "GET",
        "-f",
        "per_page=1",
    )
    if not isinstance(result, list):
        raise RuntimeError(
            "GitHub returned an unexpected Contents-read probe response; "
            f"{resource_kind} absence is unverified"
        )


async def _repository_is_empty(
    client: GhClient,
    owner: str,
    repo: str,
    *,
    resource_kind: str = "ref",
) -> bool:
    """Read GitHub's authoritative repository-empty flag for exact-object 409 classification."""

    result = await client.run(
        "repo",
        "view",
        f"{owner}/{repo}",
        "--json",
        "isEmpty",
    )
    is_empty = result.get("isEmpty") if isinstance(result, dict) else None
    if not isinstance(is_empty, bool):
        raise RuntimeError(
            "GitHub returned no authoritative repository-empty state; "
            f"{resource_kind} absence is unverified"
        )
    return is_empty


@mcp.tool(
    title="Get exact Git reference",
    description=(
        "Read-only: resolve one exact branch or tag reference path such as heads/main or "
        "tags/v1.0.0. This tool never performs matching-reference or prefix discovery. "
        "Annotated tag objects retain their tag-object identity and are peeled through "
        "bounded exact tag-object reads to a commit when applicable."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_ref(
    owner: Annotated[
        str,
        Field(
            description="GitHub repository owner or organization login.",
            min_length=1,
            max_length=39,
            pattern=OWNER_RE.pattern,
        ),
    ],
    repo: Annotated[
        str,
        Field(
            description="GitHub repository name without the owner prefix.",
            min_length=1,
            max_length=100,
            pattern=REPO_RE.pattern,
        ),
    ],
    ref: Annotated[
        str,
        Field(
            description=(
                "One exact Git ref path relative to refs/, formatted as heads/<branch> or "
                "tags/<tag>. Do not pass refs/, a bare branch/tag name, or a matching prefix."
            ),
            min_length=6,
            max_length=1024,
            pattern=r"^(?:heads|tags)/.+$",
        ),
    ],
    *,
    ctx: Context[AppContext],
) -> GitRefInfo:
    """Resolve exactly one branch/tag ref while preserving immutable Git object identity."""

    logger.info("MCP tool invocation reached server: tool=gh_get_ref")
    request = GitRefInput(owner=owner, repo=repo, ref=ref)
    validate_repository(request.owner, request.repo)
    app = app_from_context(ctx)
    expected_ref = f"refs/{request.ref}"
    ref_path = quote(request.ref, safe="/")

    try:
        payload = await app.client.run(
            "api",
            f"repos/{request.owner}/{request.repo}/git/ref/{ref_path}",
            "-X",
            "GET",
        )
    except GitHubRequestError as error:
        if error.status_code == 404:
            await _confirm_contents_read_access(app.client, request.owner, request.repo)
        elif error.status_code != 409 or not await _repository_is_empty(
            app.client, request.owner, request.repo
        ):
            raise
        return GitRefInfo(ref=expected_ref, found=False)

    if not isinstance(payload, dict):
        raise RuntimeError("GitHub exact-reference lookup returned a non-object response")
    returned_ref = payload.get("ref")
    if returned_ref != expected_ref:
        raise RuntimeError(
            "GitHub exact-reference lookup returned a different ref; refusing ambiguous evidence"
        )
    object_type, object_sha, object_url = _parse_git_object(
        payload.get("object"),
        resource=expected_ref,
    )
    peeled_commit_sha = (
        await _peel_annotated_tag(app.client, request.owner, request.repo, object_sha)
        if object_type == "tag"
        else None
    )
    return GitRefInfo(
        ref=expected_ref,
        found=True,
        object_type=object_type,
        object_sha=object_sha,
        object_url=object_url,
        peeled_commit_sha=peeled_commit_sha,
    )


@mcp.tool(
    title="Get exact Git commit",
    description=(
        "Read-only: return immutable identity and commit-object evidence for one exact "
        "40-character commit SHA. The result includes the tree SHA, every parent SHA, "
        "author, committer, message, and GitHub's verification/signature metadata without "
        "reinterpreting or upgrading that verification state."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_commit(
    owner: Annotated[
        str,
        Field(
            description="GitHub repository owner or organization login.",
            min_length=1,
            max_length=39,
            pattern=OWNER_RE.pattern,
        ),
    ],
    repo: Annotated[
        str,
        Field(
            description="GitHub repository name without the owner prefix.",
            min_length=1,
            max_length=100,
            pattern=REPO_RE.pattern,
        ),
    ],
    commit_sha: Annotated[
        str,
        Field(
            description="Exact 40-character hexadecimal Git commit SHA.",
            pattern=r"^[0-9A-Fa-f]{40}$",
        ),
    ],
    *,
    ctx: Context[AppContext],
) -> GitCommitInfo:
    """Read one exact Git commit object without revision guessing or prefix matching."""

    logger.info("MCP tool invocation reached server: tool=gh_get_commit")
    request = GitCommitInput(owner=owner, repo=repo, commit_sha=commit_sha)
    validate_repository(request.owner, request.repo)
    normalized_sha = request.commit_sha.casefold()
    app = app_from_context(ctx)

    try:
        payload = await app.client.run(
            "api",
            f"repos/{request.owner}/{request.repo}/git/commits/{normalized_sha}",
            "-X",
            "GET",
        )
    except GitHubRequestError as error:
        if error.status_code == 404:
            await _confirm_contents_read_access(
                app.client,
                request.owner,
                request.repo,
                resource_kind="commit",
            )
        elif error.status_code != 409 or not await _repository_is_empty(
            app.client,
            request.owner,
            request.repo,
            resource_kind="commit",
        ):
            raise
        return GitCommitInfo(commit_sha=normalized_sha, found=False)

    if not isinstance(payload, dict):
        raise RuntimeError("GitHub exact-commit lookup returned a non-object response")
    returned_sha = payload.get("sha")
    if (
        not isinstance(returned_sha, str)
        or not OBJECT_SHA_RE.fullmatch(returned_sha)
        or returned_sha.casefold() != normalized_sha
    ):
        raise RuntimeError(
            "GitHub exact-commit lookup did not preserve the requested commit SHA; "
            "refusing ambiguous evidence"
        )

    tree = payload.get("tree")
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str) or not OBJECT_SHA_RE.fullmatch(tree_sha):
        raise RuntimeError("GitHub exact-commit lookup returned no exact tree SHA")

    raw_parents = payload.get("parents")
    if not isinstance(raw_parents, list):
        raise RuntimeError("GitHub exact-commit lookup returned no parent list")
    parents: list[str] = []
    for parent in raw_parents:
        parent_sha = parent.get("sha") if isinstance(parent, dict) else None
        if not isinstance(parent_sha, str) or not OBJECT_SHA_RE.fullmatch(parent_sha):
            raise RuntimeError("GitHub exact-commit lookup returned a malformed parent SHA")
        parents.append(parent_sha.casefold())

    message = payload.get("message")
    if not isinstance(message, str):
        raise RuntimeError("GitHub exact-commit lookup returned no commit message")

    return GitCommitInfo(
        commit_sha=normalized_sha,
        found=True,
        tree_sha=tree_sha.casefold(),
        parents=parents,
        author=_parse_commit_person(payload.get("author"), role="author"),
        committer=_parse_commit_person(payload.get("committer"), role="committer"),
        message=message,
        verification=_parse_commit_verification(payload.get("verification")),
    )
