"""Regression coverage for GitHub API rate-limit and local governor diagnostics."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import mcp_gh_server.tools.diagnostics as diagnostics
from mcp_gh_server.rate_status_models import ApiRateStatus
from mcp_gh_server.request_governor import (
    READ_REQUEST,
    WRITE_REQUEST,
    GitHubRequestError,
    GitHubRequestGovernor,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import AppContext, gh_get_api_rate_status
from mcp_gh_server.settings import Settings


class _FakeClock:
    def __init__(self) -> None:
        self.monotonic_value = 0.0
        self.wall_value = 1_000.0

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> float:
        return self.wall_value

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall_value += seconds


def _governor(clock: _FakeClock, **kwargs: Any) -> GitHubRequestGovernor:
    return GitHubRequestGovernor(
        monotonic_clock=clock.monotonic,
        wall_clock=clock.wall,
        sleep=clock.sleep,
        **kwargs,
    )


@dataclass
class _FakeRateClient:
    governor: GitHubRequestGovernor
    results: list[GitHubRequestResult[Any] | BaseException]
    calls: list[tuple[str, ...]] = field(default_factory=list)


async def _fake_stream_governed_bytes(
    client: _FakeRateClient,
    *args: str,
    on_chunk: Any,
    timeout: float | None = None,
) -> GitHubRequestMetadata:
    assert timeout is None

    async def operation() -> GitHubRequestResult[None]:
        client.calls.append(args)
        value = client.results.pop(0)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value.value, dict):
            on_chunk(json.dumps(value.value).encode())
        return GitHubRequestResult(value=None, metadata=value.metadata)

    return (await client.governor.execute(READ_REQUEST, operation)).metadata


def _patch_transport(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    monkeypatch.setattr(diagnostics, "stream_governed_bytes", _fake_stream_governed_bytes)
    monkeypatch.setattr(diagnostics, "monotonic", clock.monotonic)


def _context(client: _FakeRateClient, **settings: Any) -> Any:
    app = AppContext(client=client, settings=Settings(**settings))  # type: ignore[arg-type]
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _rate_result(
    *,
    remaining: int,
    reset_epoch: int,
    request_id: str,
    limit: int = 5_000,
    used: int | None = None,
) -> GitHubRequestResult[dict[str, Any]]:
    actual_used = limit - remaining if used is None else used
    return GitHubRequestResult(
        value={
            "resources": {
                "core": {
                    "limit": limit,
                    "used": actual_used,
                    "remaining": remaining,
                    "reset": reset_epoch,
                }
            }
        },
        metadata=GitHubRequestMetadata(
            request_id=request_id,
            rate_limit_resource="core",
            rate_limit_limit=limit,
            rate_limit_remaining=remaining,
            rate_limit_used=actual_used,
            rate_limit_reset_epoch=reset_epoch,
        ),
    )


def test_rate_status_public_model_schema_separates_github_and_governor() -> None:
    schema = ApiRateStatus.model_json_schema()
    assert set(schema["properties"]) == {"github", "governor"}

    definitions = schema["$defs"]
    assert set(definitions["GitHubApiRateObservation"]["properties"]) == {
        "request_performed",
        "cached",
        "cache_age_seconds",
        "response_body_available",
        "observed_at_epoch",
        "request_id",
        "headers",
        "primary",
        "primary_resources",
    }
    assert set(definitions["GitHubPrimaryRateLimitState"]["properties"]) == {
        "resource",
        "limit",
        "remaining",
        "used",
        "reset_epoch",
    }
    assert set(definitions["GitHubRateLimitResponseHeaders"]["properties"]) == {
        "resource",
        "limit",
        "remaining",
        "used",
        "reset_epoch",
        "retry_after_seconds",
    }
    assert set(definitions["GitHubGovernorRateStatus"]["properties"]) == {
        "observed_at_epoch",
        "reads_blocked",
        "writes_blocked",
        "writes_delayed",
        "write_delay_seconds",
        "blocked_until_epoch",
        "retry_after_seconds",
        "block_reason",
        "last_rate_event_at_epoch",
        "last_rate_request_id",
        "last_rate_warning",
    }


async def test_rate_status_reports_normal_github_capacity_separately_from_governor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    _patch_transport(monkeypatch, clock)
    client = _FakeRateClient(
        _governor(clock),
        [_rate_result(remaining=4_999, reset_epoch=1_600, request_id="req-normal")],
    )

    result = await gh_get_api_rate_status(ctx=_context(client))

    assert client.calls == [("api", "rate_limit", "-X", "GET")]
    assert result.github.request_performed is True
    assert result.github.cached is False
    assert result.github.cache_age_seconds is None
    assert result.github.response_body_available is True
    assert result.github.observed_at_epoch == pytest.approx(1_000.0)
    assert result.github.request_id == "req-normal"
    assert result.github.headers.resource == "core"
    assert result.github.headers.limit == 5_000
    assert result.github.headers.remaining == 4_999
    assert result.github.headers.used == 1
    assert result.github.headers.reset_epoch == 1_600
    assert result.github.primary is not None
    assert result.github.primary.resource == "core"
    assert result.github.primary.limit == 5_000
    assert result.github.primary.remaining == 4_999
    assert result.github.primary.used == 1
    assert result.github.primary.reset_epoch == 1_600
    assert [state.resource for state in result.github.primary_resources] == ["core"]
    assert result.governor.reads_blocked is False
    assert result.governor.writes_blocked is False
    assert result.governor.block_reason is None
    assert result.governor.last_rate_event_at_epoch is None
    assert result.governor.last_rate_request_id is None
    assert result.governor.last_rate_warning is None


async def test_exhausted_primary_blocks_diagnostics_until_reset_then_expires_stale_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    _patch_transport(monkeypatch, clock)
    client = _FakeRateClient(
        _governor(clock),
        [
            _rate_result(remaining=0, reset_epoch=1_010, request_id="req-exhausted"),
            _rate_result(remaining=4_998, reset_epoch=1_600, request_id="req-after-reset"),
        ],
    )

    exhausted = await gh_get_api_rate_status(ctx=_context(client))

    assert exhausted.github.request_performed is True
    assert exhausted.github.primary is not None
    assert exhausted.github.primary.remaining == 0
    assert exhausted.governor.reads_blocked is True
    assert exhausted.governor.writes_blocked is True
    assert exhausted.governor.block_reason == "primary_reset"
    assert exhausted.governor.blocked_until_epoch == pytest.approx(1_010.0)
    assert exhausted.governor.retry_after_seconds == pytest.approx(10.0)
    assert exhausted.governor.last_rate_event_at_epoch == pytest.approx(1_000.0)
    assert exhausted.governor.last_rate_request_id == "req-exhausted"
    assert exhausted.governor.last_rate_warning is not None
    assert "primary rate limit is exhausted" in exhausted.governor.last_rate_warning

    suppressed = await gh_get_api_rate_status(ctx=_context(client))
    assert suppressed.github.request_performed is False
    assert suppressed.github.cached is True
    assert suppressed.github.cache_age_seconds == pytest.approx(0.0)
    assert suppressed.github.request_id == "req-exhausted"
    assert suppressed.github.primary is not None
    assert len(client.calls) == 1
    assert suppressed.governor.retry_after_seconds == pytest.approx(10.0)

    clock.advance(10.0)
    after_reset = await gh_get_api_rate_status(ctx=_context(client))

    assert client.calls == [
        ("api", "rate_limit", "-X", "GET"),
        ("api", "rate_limit", "-X", "GET"),
    ]
    assert after_reset.github.request_id == "req-after-reset"
    assert after_reset.governor.reads_blocked is False
    assert after_reset.governor.writes_blocked is False
    assert after_reset.governor.blocked_until_epoch is None
    assert after_reset.governor.retry_after_seconds is None
    assert after_reset.governor.block_reason is None
    assert after_reset.governor.last_rate_event_at_epoch == pytest.approx(1_000.0)
    assert after_reset.governor.last_rate_request_id == "req-exhausted"
    assert after_reset.governor.last_rate_warning is not None
    assert "primary rate limit is exhausted" in after_reset.governor.last_rate_warning


async def test_retry_after_response_is_retained_without_allowing_polling_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    _patch_transport(monkeypatch, clock)
    client = _FakeRateClient(
        _governor(clock),
        [
            GitHubRequestError(
                "secondary rate limit",
                rate_limited=True,
                metadata=GitHubRequestMetadata(
                    request_id="req-retry-after",
                    retry_after_seconds=7.0,
                ),
            )
        ],
    )

    limited = await gh_get_api_rate_status(ctx=_context(client))

    assert client.calls == [("api", "rate_limit", "-X", "GET")]
    assert limited.github.request_performed is True
    assert limited.github.cached is False
    assert limited.github.response_body_available is False
    assert limited.github.request_id == "req-retry-after"
    assert limited.github.headers.retry_after_seconds == pytest.approx(7.0)
    assert limited.governor.block_reason == "retry_after"
    assert limited.governor.retry_after_seconds == pytest.approx(7.0)
    assert limited.governor.last_rate_request_id == "req-retry-after"
    assert limited.governor.last_rate_warning is not None
    assert "rate-limit or abuse" in limited.governor.last_rate_warning

    suppressed = await gh_get_api_rate_status(ctx=_context(client))
    assert suppressed.github.request_performed is False
    assert suppressed.github.cached is False
    assert suppressed.github.response_body_available is False
    assert len(client.calls) == 1
    assert suppressed.governor.retry_after_seconds == pytest.approx(7.0)


async def test_secondary_or_abuse_signal_uses_existing_fallback_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    _patch_transport(monkeypatch, clock)
    governor = _governor(clock, rate_limit_fallback_seconds=23.0)
    client = _FakeRateClient(
        governor,
        [
            GitHubRequestError(
                "abuse detection",
                rate_limited=True,
                metadata=GitHubRequestMetadata(request_id="req-abuse"),
            )
        ],
    )

    limited = await gh_get_api_rate_status(ctx=_context(client))

    assert limited.github.request_performed is True
    assert limited.github.request_id == "req-abuse"
    assert limited.governor.block_reason == "fallback"
    assert limited.governor.retry_after_seconds == pytest.approx(23.0)
    assert limited.governor.last_rate_warning is not None
    assert "rate-limit or abuse" in limited.governor.last_rate_warning
    assert "fallback cooldown" in limited.governor.last_rate_warning

    clock.advance(23.0)
    expired = governor.rate_status()
    assert expired.reads_blocked is False
    assert expired.writes_blocked is False
    assert expired.blocked_until_epoch is None
    assert expired.retry_after_seconds is None
    assert expired.block_reason is None
    assert expired.last_rate_event_at_epoch == pytest.approx(1_000.0)
    assert expired.last_rate_request_id == "req-abuse"
    assert expired.last_rate_warning is not None
    assert "fallback cooldown" in expired.last_rate_warning


async def test_rate_status_reports_local_write_pacing_without_blocking_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    _patch_transport(monkeypatch, clock)
    governor = _governor(clock)

    async def write() -> GitHubRequestResult[str]:
        return GitHubRequestResult(value="done")

    await governor.execute(WRITE_REQUEST, write)
    client = _FakeRateClient(
        governor,
        [_rate_result(remaining=4_999, reset_epoch=1_600, request_id="req-after-write")],
    )

    result = await gh_get_api_rate_status(ctx=_context(client))

    assert result.governor.reads_blocked is False
    assert result.governor.writes_blocked is False
    assert result.governor.writes_delayed is True
    assert result.governor.write_delay_seconds == pytest.approx(1.0)
