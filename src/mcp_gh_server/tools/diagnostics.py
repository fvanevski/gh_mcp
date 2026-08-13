"""Local deployment and GitHub CLI diagnostic tools."""

from __future__ import annotations

from typing import Any, Literal, cast

from mcp.server.mcpserver import Context

from .. import __version__
from ..models import ServerInfo
from ..rate_status_models import (
    ApiRateStatus,
    GitHubApiRateObservation,
    GitHubGovernorRateStatus,
    GitHubPrimaryRateLimitState,
    GitHubRateLimitResponseHeaders,
)
from ..request_governor import (
    GitHubGovernorSnapshot,
    GitHubRequestError,
    GitHubRequestMetadata,
)
from ..tooling import READ_EXTERNAL, READ_LOCAL, AppContext, app_from_context, logger, mcp


@mcp.tool(
    title="Get MCP server version",
    description=(
        "Read-only local diagnostic: return this MCP server's deployed version, tool-schema "
        "version, transport, tool count, and write-policy status. This tool does not call "
        "GitHub, spawn a subprocess, request approval, or modify any state."
    ),
    annotations=READ_LOCAL,
)
async def gh_server_info(ctx: Context[AppContext]) -> ServerInfo:
    """Return deterministic local deployment metadata without contacting GitHub."""

    logger.info("MCP tool invocation reached server: tool=gh_server_info")
    app = app_from_context(ctx)
    return ServerInfo(
        server_version=__version__,
        tool_schema_version=__version__,
        transport=app.settings.transport,
        tool_count=len(await mcp.list_tools()),
        write_commands_enabled=app.settings.allow_write_commands,
        content_commits_enabled=app.settings.allow_content_commits,
        pr_merge_enabled=app.settings.allow_pr_merge,
    )


@mcp.tool(annotations=READ_EXTERNAL)
async def gh_info(ctx: Context[AppContext]) -> dict[str, Any]:
    """Return gh CLI version, authentication status, and active account."""

    app = app_from_context(ctx)
    auth_result = await app.client.run(
        "auth",
        "status",
        "--json",
        "hosts",
    )
    hosts = auth_result.get("hosts", {})
    version_result = await app.client.run("version", json_output=False)
    version_line = version_result.get("stdout", "") or ""
    version = (
        version_line.strip().split()[2] if len(version_line.strip().split()) > 2 else "unknown"
    )

    active_account: str | None = None
    hostname: str | None = None
    for host, accounts in hosts.items():
        if isinstance(accounts, list):
            for acct in accounts:
                if isinstance(acct, dict) and acct.get("active"):
                    active_account = acct.get("login")
                    hostname = host
                    break
        if active_account:
            break

    return {
        "version": version,
        "authenticated": active_account is not None,
        "active_account": active_account,
        "hostname": hostname,
    }


@mcp.tool(
    title="Get GitHub API rate status",
    description=(
        "Read-only diagnostic: perform one governed GET /rate_limit request and return "
        "GitHub-provided primary rate-limit evidence separately from local request-governor "
        "blocking and write-pacing state. If the governor is already rate-blocked, no GitHub "
        "request is performed."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_api_rate_status(ctx: Context[AppContext]) -> ApiRateStatus:
    """Return authoritative GitHub rate evidence plus separate local governor state."""

    app = app_from_context(ctx)
    try:
        result = await app.client.run_with_metadata("api", "rate_limit", "-X", "GET")
    except GitHubRequestError as exc:
        if not exc.rate_limited:
            raise
        governor = app.client.governor.rate_status()
        metadata = GitHubRequestMetadata() if exc.governor_blocked else exc.metadata
        return ApiRateStatus(
            github=_github_rate_observation(
                metadata,
                payload=None,
                observed_at_epoch=governor.observed_at_epoch,
                request_performed=not exc.governor_blocked,
            ),
            governor=_governor_rate_status(governor),
        )

    governor = app.client.governor.rate_status()
    return ApiRateStatus(
        github=_github_rate_observation(
            result.metadata,
            payload=result.value,
            observed_at_epoch=governor.observed_at_epoch,
            request_performed=True,
        ),
        governor=_governor_rate_status(governor),
    )


def _github_rate_observation(
    metadata: GitHubRequestMetadata,
    *,
    payload: Any,
    observed_at_epoch: float,
    request_performed: bool,
) -> GitHubApiRateObservation:
    return GitHubApiRateObservation(
        request_performed=request_performed,
        response_body_available=isinstance(payload, dict),
        observed_at_epoch=observed_at_epoch,
        request_id=metadata.request_id if request_performed else None,
        headers=GitHubRateLimitResponseHeaders(
            resource=metadata.rate_limit_resource if request_performed else None,
            limit=metadata.rate_limit_limit if request_performed else None,
            remaining=metadata.rate_limit_remaining if request_performed else None,
            used=metadata.rate_limit_used if request_performed else None,
            reset_epoch=metadata.rate_limit_reset_epoch if request_performed else None,
            retry_after_seconds=metadata.retry_after_seconds if request_performed else None,
        ),
        primary=_primary_rate_limit(payload) if request_performed else None,
    )


def _primary_rate_limit(payload: Any) -> GitHubPrimaryRateLimitState | None:
    if not isinstance(payload, dict):
        return None
    resources = payload.get("resources")
    if not isinstance(resources, dict):
        return None
    core = resources.get("core")
    if not isinstance(core, dict):
        return None
    return GitHubPrimaryRateLimitState(
        limit=_nonnegative_int(core.get("limit")),
        remaining=_nonnegative_int(core.get("remaining")),
        used=_nonnegative_int(core.get("used")),
        reset_epoch=_nonnegative_int(core.get("reset")),
    )


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _governor_rate_status(snapshot: GitHubGovernorSnapshot) -> GitHubGovernorRateStatus:
    return GitHubGovernorRateStatus(
        observed_at_epoch=snapshot.observed_at_epoch,
        reads_blocked=snapshot.reads_blocked,
        writes_blocked=snapshot.writes_blocked,
        writes_delayed=snapshot.writes_delayed,
        write_delay_seconds=snapshot.write_delay_seconds,
        blocked_until_epoch=snapshot.blocked_until_epoch,
        retry_after_seconds=snapshot.retry_after_seconds,
        block_reason=cast(
            Literal["retry_after", "primary_reset", "fallback"] | None,
            snapshot.block_reason,
        ),
        last_rate_event_at_epoch=snapshot.last_rate_event_at_epoch,
        last_request_id=snapshot.last_request_id,
        last_warning=snapshot.last_warning,
    )
