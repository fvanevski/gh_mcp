"""Read-only GitHub Actions artifact metadata tools."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from ..evidence import pagination_evidence
from ..models import WorkflowArtifact, WorkflowArtifactsPage
from ..tooling import (
    OBJECT_SHA_RE,
    READ_EXTERNAL,
    AppContext,
    app_from_context,
    logger,
    mcp,
    validate_repository,
)

_GITHUB_ARTIFACTS_PER_PAGE_MAX = 100


async def _get_workflow_run_identity(
    app: AppContext,
    owner: str,
    repo: str,
    run_id: int,
) -> tuple[int, str]:
    """Read one workflow run and return its current attempt and immutable head SHA."""

    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/runs/{run_id}",
        "-X",
        "GET",
    )
    if not isinstance(result, dict):
        raise RuntimeError("GitHub did not return structured workflow-run metadata")

    actual_run_id = result.get("id")
    if (
        not isinstance(actual_run_id, int)
        or isinstance(actual_run_id, bool)
        or actual_run_id < 1
    ):
        raise RuntimeError("GitHub returned a workflow run without a valid id")
    if actual_run_id != run_id:
        raise RuntimeError(
            f"Workflow run identity mismatch: GitHub returned run {actual_run_id}, "
            f"expected {run_id}"
        )

    attempt = result.get("run_attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise RuntimeError("GitHub returned a workflow run without a valid run_attempt")

    head_sha = result.get("head_sha")
    if not isinstance(head_sha, str) or not OBJECT_SHA_RE.fullmatch(head_sha):
        raise RuntimeError("GitHub returned a workflow run without a valid head SHA")

    return attempt, head_sha.casefold()


def _artifact_from_payload(
    payload: Any,
    *,
    expected_artifact_id: int | None = None,
    expected_run_id: int | None = None,
    expected_head_sha: str | None = None,
    exact_name: str | None = None,
) -> WorkflowArtifact:
    """Validate one artifact record without weakening identity or filter semantics."""

    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned a malformed artifact record")

    artifact_id = payload.get("id")
    if not isinstance(artifact_id, int) or isinstance(artifact_id, bool) or artifact_id < 1:
        raise RuntimeError("GitHub returned an artifact without a valid id")
    if expected_artifact_id is not None and artifact_id != expected_artifact_id:
        raise RuntimeError(
            f"Artifact identity mismatch: GitHub returned artifact {artifact_id}, "
            f"expected {expected_artifact_id}"
        )

    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("GitHub returned an artifact without a valid name")
    if exact_name is not None and name != exact_name:
        raise RuntimeError(
            f"Artifact name mismatch: GitHub returned {name!r}, expected exact name {exact_name!r}"
        )

    size_in_bytes = payload.get("size_in_bytes")
    if (
        not isinstance(size_in_bytes, int)
        or isinstance(size_in_bytes, bool)
        or size_in_bytes < 0
    ):
        raise RuntimeError("GitHub returned an artifact without a valid size_in_bytes")

    expired = payload.get("expired")
    if not isinstance(expired, bool):
        raise RuntimeError("GitHub returned an artifact without a valid expired flag")

    created_at = payload.get("created_at")
    expires_at = payload.get("expires_at")
    if not isinstance(created_at, str) or not created_at:
        raise RuntimeError("GitHub returned an artifact without a valid created_at")
    if not isinstance(expires_at, str) or not expires_at:
        raise RuntimeError("GitHub returned an artifact without a valid expires_at")

    digest = payload.get("digest")
    if digest is not None and (not isinstance(digest, str) or not digest):
        raise RuntimeError("GitHub returned an artifact with an invalid digest")

    workflow_run = payload.get("workflow_run")
    if not isinstance(workflow_run, dict):
        raise RuntimeError("GitHub artifact metadata omitted workflow-run identity")

    workflow_run_id = workflow_run.get("id")
    if (
        not isinstance(workflow_run_id, int)
        or isinstance(workflow_run_id, bool)
        or workflow_run_id < 1
    ):
        raise RuntimeError("GitHub artifact metadata contained an invalid workflow run id")

    workflow_head_sha = workflow_run.get("head_sha")
    if not isinstance(workflow_head_sha, str) or not OBJECT_SHA_RE.fullmatch(workflow_head_sha):
        raise RuntimeError("GitHub artifact metadata contained an invalid workflow head SHA")
    normalized_head_sha = workflow_head_sha.casefold()

    if expected_run_id is not None and workflow_run_id != expected_run_id:
        raise RuntimeError(
            f"Artifact/run identity mismatch: artifact {artifact_id} belongs to run "
            f"{workflow_run_id}, expected {expected_run_id}"
        )
    if expected_head_sha is not None and normalized_head_sha != expected_head_sha.casefold():
        raise RuntimeError(
            f"Artifact/head identity mismatch: artifact {artifact_id} reports head "
            f"{normalized_head_sha}, expected {expected_head_sha.casefold()}"
        )

    return WorkflowArtifact(
        id=artifact_id,
        name=name,
        size_in_bytes=size_in_bytes,
        digest=digest,
        expired=expired,
        created_at=created_at,
        expires_at=expires_at,
        workflow_run_id=workflow_run_id,
        workflow_head_sha=normalized_head_sha,
    )


@mcp.tool(
    title="List workflow run artifacts",
    description=(
        "Read-only: return one bounded page of immutable GitHub Actions artifact metadata "
        "for an exact workflow run. Optional name filtering is exact; archives are never "
        "downloaded and GitHub is never modified."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_list_run_artifacts(
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
    run_id: Annotated[int, Field(ge=1, description="Exact workflow run identifier.")],
    *,
    ctx: Context[AppContext],
    page: Annotated[int, Field(ge=1, le=10_000, description="One-based artifact page.")] = 1,
    per_page: Annotated[
        int | None,
        Field(ge=1, le=100, description="Artifacts per page, capped by server policy."),
    ] = None,
    name: Annotated[
        str | None,
        Field(
            min_length=1,
            description="Exact artifact name; GitHub must return only literal matches.",
        ),
    ] = None,
) -> WorkflowArtifactsPage:
    """Return a bounded artifact page pinned to one workflow run attempt/head identity."""

    logger.info("MCP tool invocation reached server: tool=gh_list_run_artifacts")
    app = app_from_context(ctx)
    validate_repository(owner, repo)

    attempt, head_sha = await _get_workflow_run_identity(app, owner, repo, run_id)
    requested_per_page = app.settings.default_max_results if per_page is None else per_page
    hard_per_page = min(app.settings.hard_max_results, _GITHUB_ARTIFACTS_PER_PAGE_MAX)
    limit = min(requested_per_page, hard_per_page)

    args = [
        "api",
        f"repos/{owner}/{repo}/actions/runs/{run_id}/artifacts",
        "-X",
        "GET",
        "-f",
        f"page={page}",
        "-f",
        f"per_page={limit}",
    ]
    if name is not None:
        args.extend(["-f", f"name={name}"])

    result = await app.client.run(*args)
    if not isinstance(result, dict):
        raise RuntimeError("GitHub did not return structured workflow artifact metadata")
    raw_artifacts = result.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise RuntimeError("GitHub did not return a workflow artifact list")
    total_count = result.get("total_count")
    if not isinstance(total_count, int) or isinstance(total_count, bool) or total_count < 0:
        raise RuntimeError("GitHub did not return a valid workflow artifact count")

    artifacts = [
        _artifact_from_payload(
            item,
            expected_run_id=run_id,
            expected_head_sha=head_sha,
            exact_name=name,
        )
        for item in raw_artifacts
    ]

    verified_attempt, verified_head_sha = await _get_workflow_run_identity(
        app, owner, repo, run_id
    )
    if (verified_attempt, verified_head_sha) != (attempt, head_sha):
        raise RuntimeError(
            "Workflow run attempt/head identity changed during the artifact metadata read; retry"
        )

    evidence = pagination_evidence(
        page=page,
        requested_per_page=per_page,
        default_per_page=app.settings.default_max_results,
        hard_max_results=hard_per_page,
        returned_count=len(artifacts),
        total_count=total_count,
    )
    return WorkflowArtifactsPage(
        run_id=run_id,
        attempt=attempt,
        head_sha=head_sha,
        total_count=total_count,
        page=evidence.page,
        per_page=evidence.per_page,
        has_more=evidence.has_more,
        truncated=evidence.truncated,
        warning=evidence.warning,
        artifacts=artifacts,
    )


@mcp.tool(
    title="Get workflow artifact metadata",
    description=(
        "Read-only: return metadata for one exact GitHub Actions artifact identifier, "
        "including digest, expiry, and workflow-run/head identity. The artifact archive "
        "is never downloaded and GitHub is never modified."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_artifact(
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
    artifact_id: Annotated[int, Field(ge=1, description="Exact workflow artifact identifier.")],
    *,
    ctx: Context[AppContext],
) -> WorkflowArtifact:
    """Return one exact artifact record without requiring archive or run availability."""

    logger.info("MCP tool invocation reached server: tool=gh_get_artifact")
    app = app_from_context(ctx)
    validate_repository(owner, repo)

    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/artifacts/{artifact_id}",
        "-X",
        "GET",
    )
    return _artifact_from_payload(result, expected_artifact_id=artifact_id)
