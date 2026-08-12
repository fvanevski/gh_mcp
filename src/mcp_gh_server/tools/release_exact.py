"""Exact-target guarded GitHub release creation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from ..models import GitRefInput
from ..release_exact_models import ReleaseExactResult
from ..request_governor import GitHubRequestError, GitHubRequestResult
from ..tooling import (
    ADD_EXTERNAL,
    OBJECT_SHA_RE,
    OWNER_RE,
    REPO_RE,
    AppContext,
    app_from_context,
    logger,
    mcp,
    require_write_enabled,
)
from ..write_contracts import (
    WritePrecondition,
    WritePreconditionMismatch,
    execute_write_readback,
    require_write_precondition,
    run_api_json_write_with_metadata,
)
from .git import gh_get_commit, gh_get_ref


@dataclass(frozen=True, slots=True)
class _ReleaseRecord:
    release_id: int
    tag_name: str
    url: str
    name: str | None
    body: str | None
    draft: bool
    prerelease: bool


@dataclass(frozen=True, slots=True)
class _ReleaseReadback:
    release: _ReleaseRecord
    tag_commit_sha: str
    is_latest: bool


def _parse_release(raw: object, *, resource: str) -> _ReleaseRecord:
    """Validate the release fields required for exact semantic readback."""

    if not isinstance(raw, dict):
        raise RuntimeError(f"GitHub returned a non-object {resource}")
    release_id = raw.get("id")
    tag_name = raw.get("tag_name")
    url = raw.get("html_url")
    name = raw.get("name")
    body = raw.get("body")
    draft = raw.get("draft")
    prerelease = raw.get("prerelease")
    if not isinstance(release_id, int) or release_id < 1:
        raise RuntimeError(f"GitHub returned no positive release id for {resource}")
    if not isinstance(tag_name, str) or not tag_name:
        raise RuntimeError(f"GitHub returned no release tag name for {resource}")
    if not isinstance(url, str) or not url:
        raise RuntimeError(f"GitHub returned no release URL for {resource}")
    if name is not None and not isinstance(name, str):
        raise RuntimeError(f"GitHub returned a malformed release name for {resource}")
    if body is not None and not isinstance(body, str):
        raise RuntimeError(f"GitHub returned a malformed release body for {resource}")
    if not isinstance(draft, bool) or not isinstance(prerelease, bool):
        raise RuntimeError(f"GitHub returned malformed release mode fields for {resource}")
    return _ReleaseRecord(
        release_id=release_id,
        tag_name=tag_name,
        url=url,
        name=name,
        body=body,
        draft=draft,
        prerelease=prerelease,
    )


async def _confirm_release_read_access(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
) -> None:
    """Prove release-read access before classifying an exact release 404 as absence."""

    app = app_from_context(ctx)
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/releases",
        "-X",
        "GET",
        "-f",
        "per_page=1",
    )
    if not isinstance(result, list):
        raise RuntimeError(
            "GitHub returned an unexpected releases-read probe response; "
            "release absence is unverified"
        )


async def _read_release_by_tag(
    owner: str,
    repo: str,
    tag_name: str,
    *,
    ctx: Context[AppContext],
) -> _ReleaseRecord | None:
    """Read one exact release by tag, with permission-safe absence classification."""

    app = app_from_context(ctx)
    tag_path = quote(tag_name, safe="")
    try:
        raw = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/releases/tags/{tag_path}",
            "-X",
            "GET",
        )
    except GitHubRequestError as error:
        if error.status_code != 404:
            raise
        await _confirm_release_read_access(owner, repo, ctx=ctx)
        return None
    return _parse_release(raw, resource=f"release {tag_name!r}")


async def _read_latest_release_id(
    owner: str,
    repo: str,
    *,
    ctx: Context[AppContext],
) -> int | None:
    """Return the current latest published release id, or prove that none exists."""

    app = app_from_context(ctx)
    try:
        raw = await app.client.run(
            "api",
            f"repos/{owner}/{repo}/releases/latest",
            "-X",
            "GET",
        )
    except GitHubRequestError as error:
        if error.status_code != 404:
            raise
        await _confirm_release_read_access(owner, repo, ctx=ctx)
        return None
    return _parse_release(raw, resource="latest release").release_id


async def _read_tag_commit_sha(
    owner: str,
    repo: str,
    tag_name: str,
    *,
    ctx: Context[AppContext],
) -> str | None:
    """Resolve one exact release tag to its commit without accepting non-commit targets."""

    result = await gh_get_ref(owner, repo, f"tags/{tag_name}", ctx=ctx)
    if not result.found:
        return None
    if result.object_type == "commit" and result.object_sha is not None:
        return result.object_sha
    if result.object_type == "tag" and result.peeled_commit_sha is not None:
        return result.peeled_commit_sha
    raise RuntimeError(
        f"release tag refs/tags/{tag_name} does not resolve authoritatively to a commit"
    )


async def _read_target_commit_sha(
    owner: str,
    repo: str,
    expected_target_sha: str,
    *,
    ctx: Context[AppContext],
) -> str | None:
    """Prove one exact target commit exists without branch or revision-name resolution."""

    result = await gh_get_commit(owner, repo, expected_target_sha, ctx=ctx)
    return result.commit_sha if result.found else None


@mcp.tool(
    title="Create release at exact target",
    description=(
        "Additive write: create one GitHub release using an exact 40-character target commit "
        "SHA. The tool verifies target identity, optionally requires the tag and release to be "
        "absent, performs exactly one governed release-creation request, and then verifies the "
        "created tag commit, release state, and explicit latest/non-latest state. It never "
        "retries an ambiguous release mutation automatically."
    ),
    annotations=ADD_EXTERNAL,
)
async def gh_create_release_exact(
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
    tag_name: Annotated[
        str,
        Field(
            description="Exact Git tag name to create the release for.",
            min_length=1,
            max_length=1019,
        ),
    ],
    expected_target_sha: Annotated[
        str,
        Field(
            description="Exact 40-character commit SHA at which the release tag must resolve.",
            pattern=r"^[0-9A-Fa-f]{40}$",
        ),
    ],
    make_latest: Annotated[
        bool,
        Field(
            description=(
                "Explicit latest-release policy. Pass false for release candidates, "
                "prereleases, and any release that must not become Latest."
            )
        ),
    ],
    *,
    ctx: Context[AppContext],
    name: str | None = None,
    body: str | None = None,
    draft: bool = False,
    prerelease: bool = False,
    expected_tag_absent: bool = True,
    expected_release_absent: bool = True,
) -> ReleaseExactResult:
    """Create one exact-target release and verify its authoritative GitHub state."""

    logger.info("MCP tool invocation reached server: tool=gh_create_release_exact")
    GitRefInput(owner=owner, repo=repo, ref=f"tags/{tag_name}")
    if not OBJECT_SHA_RE.fullmatch(expected_target_sha):
        raise ValueError("expected_target_sha must be exactly 40 hexadecimal characters")
    if make_latest and (draft or prerelease):
        raise ValueError("draft or prerelease releases cannot be marked as latest")

    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="release_create")
    normalized_target_sha = expected_target_sha.casefold()
    resolved_target_sha = normalized_target_sha

    async def read_target_sha() -> str | None:
        nonlocal resolved_target_sha
        current = await _read_target_commit_sha(
            owner,
            repo,
            normalized_target_sha,
            ctx=ctx,
        )
        if current is not None:
            resolved_target_sha = current
        return current

    async def read_tag_sha() -> str | None:
        return await _read_tag_commit_sha(owner, repo, tag_name, ctx=ctx)

    async def read_release_id() -> int | None:
        release = await _read_release_by_tag(owner, repo, tag_name, ctx=ctx)
        return release.release_id if release is not None else None

    async def precondition() -> WritePrecondition[str | None]:
        await require_write_precondition(
            read_target_sha,
            normalized_target_sha,
            label=f"release target commit {normalized_target_sha}",
        )

        if expected_tag_absent:
            await require_write_precondition(
                read_tag_sha,
                None,
                label=f"release tag refs/tags/{tag_name} absence",
            )
        else:
            existing_tag_sha = await read_tag_sha()
            if existing_tag_sha is not None and existing_tag_sha != normalized_target_sha:
                raise WritePreconditionMismatch(
                    f"release tag refs/tags/{tag_name} target precondition mismatch: expected "
                    f"{normalized_target_sha!r}, current {existing_tag_sha!r}; "
                    "no write was attempted"
                )

        if expected_release_absent:
            await require_write_precondition(
                read_release_id,
                None,
                label=f"release {tag_name!r} absence",
            )

        return await require_write_precondition(
            read_target_sha,
            normalized_target_sha,
            label=f"release target commit {normalized_target_sha}",
        )

    async def write() -> GitHubRequestResult[Any]:
        payload: dict[str, Any] = {
            "tag_name": tag_name,
            "target_commitish": normalized_target_sha,
            "draft": draft,
            "prerelease": prerelease,
            "make_latest": "true" if make_latest else "false",
        }
        if name is not None:
            payload["name"] = name
        if body is not None:
            payload["body"] = body
        return await run_api_json_write_with_metadata(
            app.client,
            "POST",
            f"repos/{owner}/{repo}/releases",
            payload,
        )

    async def readback() -> _ReleaseReadback:
        release = await _read_release_by_tag(owner, repo, tag_name, ctx=ctx)
        if release is None:
            raise RuntimeError(
                f"created release {tag_name!r} is absent during authoritative readback"
            )
        tag_commit_sha = await _read_tag_commit_sha(owner, repo, tag_name, ctx=ctx)
        if tag_commit_sha is None:
            raise RuntimeError(
                f"created release tag refs/tags/{tag_name} is absent during authoritative readback"
            )
        latest_release_id = await _read_latest_release_id(owner, repo, ctx=ctx)
        return _ReleaseReadback(
            release=release,
            tag_commit_sha=tag_commit_sha,
            is_latest=latest_release_id == release.release_id,
        )

    def matches_requested(value: _ReleaseReadback) -> bool:
        release = value.release
        return (
            release.tag_name == tag_name
            and value.tag_commit_sha == normalized_target_sha
            and release.draft is draft
            and release.prerelease is prerelease
            and value.is_latest is make_latest
            and (name is None or release.name == name)
            and (body is None or release.body == body)
        )

    execution = await execute_write_readback(
        resource=f"Release {tag_name!r} at {normalized_target_sha}",
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=matches_requested,
    )

    readback_value = execution.readback_value
    release = readback_value.release if readback_value is not None else None
    outcome = execution.outcome
    return ReleaseExactResult(
        tag_name=tag_name,
        expected_target_sha=normalized_target_sha,
        resolved_target_sha=resolved_target_sha,
        release_id=release.release_id if release is not None else None,
        release_url=release.url if release is not None else None,
        tag_commit_sha=readback_value.tag_commit_sha if readback_value is not None else None,
        release_name=release.name if release is not None else None,
        is_draft=release.draft if release is not None else None,
        is_prerelease=release.prerelease if release is not None else None,
        make_latest=make_latest,
        is_latest=readback_value.is_latest if readback_value is not None else None,
        precondition_checked=outcome.precondition_checked,
        write_completed=outcome.write_completed,
        readback_completed=outcome.readback_completed,
        state_matches_requested=outcome.state_matches_requested,
        warning=outcome.warning,
        request_id=outcome.request_id,
    )
