"""Local deployment and GitHub CLI diagnostic tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from weakref import WeakKeyDictionary

from mcp.server.mcpserver import Context

from .. import __version__
from ..binary_evidence import stream_governed_bytes
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
    GitHubRequestGovernor,
    GitHubRequestMetadata,
)
from ..tooling import READ_EXTERNAL, READ_LOCAL, AppContext, app_from_context, logger, mcp

_RATE_STATUS_MAX_BODY_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class _RateStatusObservation:
    payload: dict[str, Any]
    metadata: GitHubRequestMetadata
    observed_at_epoch: float


@dataclass(slots=True)
class _RateStatusCache:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    entry: _RateStatusObservation | None = None
    cached_at_monotonic: float | None = None


_RATE_STATUS_CACHES: WeakKeyDictionary[GitHubRequestGovernor, _RateStatusCache] = (
    WeakKeyDictionary()
)


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
        "Read-only diagnostic: perform or reuse a locally paced governed GET /rate_limit "
        "observation and return GitHub-provided primary rate-limit evidence separately from "
        "local request-governor blocking and write-pacing state. Repeated calls inside the "
        "configured diagnostic refresh interval are served from a local cache and do not "
        "create additional GitHub requests."
    ),
    annotations=READ_EXTERNAL,
)
async def gh_get_api_rate_status(ctx: Context[AppContext]) -> ApiRateStatus:
    """Return authoritative GitHub rate evidence plus separate local governor state."""

    app = app_from_context(ctx)
    cache = _rate_status_cache(app.client.governor)
    minimum_interval = float(app.settings.api_rate_status_min_interval_seconds)

    async with cache.lock:
        now_monotonic = monotonic()
        age = _cache_age(cache, now_monotonic)
        governor_before = app.client.governor.rate_status()
        if cache.entry is not None and (
            governor_before.reads_blocked or (age is not None and age < minimum_interval)
        ):
            github = _github_rate_observation(
                cache.entry.metadata,
                payload=cache.entry.payload,
                observed_at_epoch=cache.entry.observed_at_epoch,
                request_performed=False,
                cached=True,
                cache_age_seconds=age,
            )
        elif governor_before.reads_blocked:
            github = _empty_github_rate_observation()
        else:
            try:
                observation = await _perform_rate_status_request(app)
            except GitHubRequestError as exc:
                if not exc.rate_limited:
                    raise
                governor_after_error = app.client.governor.rate_status()
                if exc.governor_blocked and cache.entry is not None:
                    age = _cache_age(cache, monotonic())
                    github = _github_rate_observation(
                        cache.entry.metadata,
                        payload=cache.entry.payload,
                        observed_at_epoch=cache.entry.observed_at_epoch,
                        request_performed=False,
                        cached=True,
                        cache_age_seconds=age,
                    )
                elif exc.governor_blocked:
                    github = _empty_github_rate_observation()
                else:
                    github = _github_rate_observation(
                        exc.metadata,
                        payload=None,
                        observed_at_epoch=governor_after_error.observed_at_epoch,
                        request_performed=True,
                        cached=False,
                        cache_age_seconds=None,
                    )
            else:
                cache.entry = observation
                cache.cached_at_monotonic = monotonic()
                github = _github_rate_observation(
                    observation.metadata,
                    payload=observation.payload,
                    observed_at_epoch=observation.observed_at_epoch,
                    request_performed=True,
                    cached=False,
                    cache_age_seconds=None,
                )

    governor = app.client.governor.rate_status()
    return ApiRateStatus(github=github, governor=_governor_rate_status(governor))


def _rate_status_cache(governor: GitHubRequestGovernor) -> _RateStatusCache:
    cache = _RATE_STATUS_CACHES.get(governor)
    if cache is None:
        cache = _RateStatusCache()
        _RATE_STATUS_CACHES[governor] = cache
    return cache


def _cache_age(cache: _RateStatusCache, now_monotonic: float) -> float | None:
    cached_at = cache.cached_at_monotonic
    if cached_at is None:
        return None
    return max(now_monotonic - cached_at, 0.0)


async def _perform_rate_status_request(app: AppContext) -> _RateStatusObservation:
    body = bytearray()

    def collect(chunk: bytes) -> None:
        if len(body) + len(chunk) > _RATE_STATUS_MAX_BODY_BYTES:
            raise RuntimeError(
                "GitHub /rate_limit response exceeded the "
                f"{_RATE_STATUS_MAX_BODY_BYTES}-byte hard limit"
            )
        body.extend(chunk)

    metadata = await stream_governed_bytes(
        app.client,
        "api",
        "rate_limit",
        "-X",
        "GET",
        on_chunk=collect,
    )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("GitHub /rate_limit returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub /rate_limit returned a non-object JSON payload")
    observed_at = app.client.governor.rate_status().observed_at_epoch
    return _RateStatusObservation(payload=payload, metadata=metadata, observed_at_epoch=observed_at)


def _empty_github_rate_observation() -> GitHubApiRateObservation:
    return GitHubApiRateObservation(
        request_performed=False,
        cached=False,
        cache_age_seconds=None,
        response_body_available=False,
        observed_at_epoch=None,
        request_id=None,
        headers=GitHubRateLimitResponseHeaders(),
        primary=None,
        primary_resources=[],
    )


def _github_rate_observation(
    metadata: GitHubRequestMetadata,
    *,
    payload: Any,
    observed_at_epoch: float | None,
    request_performed: bool,
    cached: bool,
    cache_age_seconds: float | None,
) -> GitHubApiRateObservation:
    primary_resources = _primary_rate_limits(payload)
    core = next((state for state in primary_resources if state.resource == "core"), None)
    return GitHubApiRateObservation(
        request_performed=request_performed,
        cached=cached,
        cache_age_seconds=cache_age_seconds,
        response_body_available=isinstance(payload, dict),
        observed_at_epoch=observed_at_epoch,
        request_id=metadata.request_id if request_performed or cached else None,
        headers=GitHubRateLimitResponseHeaders(
            resource=metadata.rate_limit_resource if request_performed or cached else None,
            limit=metadata.rate_limit_limit if request_performed or cached else None,
            remaining=metadata.rate_limit_remaining if request_performed or cached else None,
            used=metadata.rate_limit_used if request_performed or cached else None,
            reset_epoch=metadata.rate_limit_reset_epoch if request_performed or cached else None,
            retry_after_seconds=metadata.retry_after_seconds
            if request_performed or cached
            else None,
        ),
        primary=core,
        primary_resources=primary_resources if request_performed or cached else [],
    )


def _primary_rate_limits(payload: Any) -> list[GitHubPrimaryRateLimitState]:
    if not isinstance(payload, dict):
        return []
    resources = payload.get("resources")
    if not isinstance(resources, dict):
        return []

    states: list[GitHubPrimaryRateLimitState] = []
    for resource, raw_state in sorted(resources.items(), key=lambda item: str(item[0])):
        if not isinstance(resource, str) or not resource or not isinstance(raw_state, dict):
            continue
        limit = _nonnegative_int(raw_state.get("limit"))
        remaining = _nonnegative_int(raw_state.get("remaining"))
        used = _nonnegative_int(raw_state.get("used"))
        reset_epoch = _nonnegative_int(raw_state.get("reset"))
        if all(value is None for value in (limit, remaining, used, reset_epoch)):
            continue
        states.append(
            GitHubPrimaryRateLimitState(
                resource=resource,
                limit=limit,
                remaining=remaining,
                used=used,
                reset_epoch=reset_epoch,
            )
        )
    return states


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
        block_reason=snapshot.block_reason,
        last_rate_event_at_epoch=snapshot.last_rate_event_at_epoch,
        last_rate_request_id=snapshot.last_rate_request_id,
        last_rate_warning=snapshot.last_rate_warning,
    )
