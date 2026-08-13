"""Boundary tests for paced API-rate diagnostics and resource provenance."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import mcp_gh_server.tools.diagnostics as diagnostics
from mcp_gh_server.request_governor import (
    READ_REQUEST,
    GitHubRequestGovernor,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.server import AppContext, gh_get_api_rate_status
from mcp_gh_server.settings import Settings


class _FakeClock:
    def __init__(self) -> None:
        self.monotonic_value = 0.0
        self.wall_value = 2_000.0

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> float:
        return self.wall_value

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall_value += seconds


@dataclass
class _FakeRateClient:
    governor: GitHubRequestGovernor
    payload: dict[str, Any]
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
        request_number = len(client.calls)
        on_chunk(json.dumps(client.payload).encode())
        return GitHubRequestResult(
            value=None,
            metadata=GitHubRequestMetadata(
                request_id=f"req-{request_number}",
                rate_limit_resource="core",
                rate_limit_limit=5_000,
                rate_limit_remaining=4_990,
                rate_limit_used=10,
                rate_limit_reset_epoch=2_600,
            ),
        )

    return (await client.governor.execute(READ_REQUEST, operation)).metadata


def _context(client: _FakeRateClient, settings: Settings) -> Any:
    app = AppContext(client=client, settings=settings)  # type: ignore[arg-type]
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


async def test_rate_status_coalesces_unblocked_polling_and_reports_all_resource_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "resources": {
            "core": {"limit": 5_000, "used": 10, "remaining": 4_990, "reset": 2_600},
            "search": {"limit": 30, "used": 2, "remaining": 28, "reset": 2_300},
            "graphql": {"limit": 5_000, "used": 20, "remaining": 4_980, "reset": 2_600},
            "future_resource": {"limit": 12, "used": 1, "remaining": 11, "reset": 2_400},
        }
    }
    clock = _FakeClock()
    monkeypatch.setattr(diagnostics, "stream_governed_bytes", _fake_stream_governed_bytes)
    monkeypatch.setattr(diagnostics, "monotonic", clock.monotonic)
    governor = GitHubRequestGovernor(
        monotonic_clock=clock.monotonic,
        wall_clock=clock.wall,
        sleep=clock.sleep,
    )
    settings = Settings(api_rate_status_min_interval_seconds=5.0)
    client = _FakeRateClient(governor=governor, payload=payload)
    ctx = _context(client, settings)

    first = await gh_get_api_rate_status(ctx=ctx)

    assert client.calls == [("api", "rate_limit", "-X", "GET")]
    assert first.github.request_performed is True
    assert first.github.cached is False
    assert first.github.cache_age_seconds is None
    assert first.github.observed_at_epoch == pytest.approx(2_000.0)
    assert first.github.request_id == "req-1"
    assert first.github.headers.resource == "core"
    assert first.github.headers.limit == 5_000
    assert first.github.headers.remaining == 4_990
    assert first.github.headers.used == 10
    assert first.github.headers.reset_epoch == 2_600
    assert [state.resource for state in first.github.primary_resources] == [
        "core",
        "future_resource",
        "graphql",
        "search",
    ]
    assert first.github.primary is not None
    assert first.github.primary.resource == "core"

    second = await gh_get_api_rate_status(ctx=ctx)

    assert len(client.calls) == 1
    assert second.github.request_performed is False
    assert second.github.cached is True
    assert second.github.cache_age_seconds == pytest.approx(0.0)
    assert second.github.response_body_available is True
    assert second.github.observed_at_epoch == pytest.approx(2_000.0)
    assert second.github.request_id == "req-1"
    assert [state.resource for state in second.github.primary_resources] == [
        "core",
        "future_resource",
        "graphql",
        "search",
    ]

    clock.advance(4.999)
    third = await gh_get_api_rate_status(ctx=ctx)
    assert len(client.calls) == 1
    assert third.github.cached is True
    assert third.github.cache_age_seconds == pytest.approx(4.999)

    clock.advance(0.001)
    refreshed = await gh_get_api_rate_status(ctx=ctx)

    assert len(client.calls) == 2
    assert refreshed.github.request_performed is True
    assert refreshed.github.cached is False
    assert refreshed.github.request_id == "req-2"
    assert refreshed.github.observed_at_epoch == pytest.approx(2_005.0)


async def test_rate_status_coalesces_concurrent_diagnostic_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    payload = {
        "resources": {
            "core": {"limit": 5_000, "used": 1, "remaining": 4_999, "reset": 2_600}
        }
    }
    clock = _FakeClock()
    monkeypatch.setattr(diagnostics, "stream_governed_bytes", _fake_stream_governed_bytes)
    monkeypatch.setattr(diagnostics, "monotonic", clock.monotonic)
    governor = GitHubRequestGovernor(
        monotonic_clock=clock.monotonic,
        wall_clock=clock.wall,
        sleep=clock.sleep,
    )
    settings = Settings(api_rate_status_min_interval_seconds=5.0)
    client = _FakeRateClient(governor=governor, payload=payload)
    ctx = _context(client, settings)

    first, second = await asyncio.gather(
        gh_get_api_rate_status(ctx=ctx),
        gh_get_api_rate_status(ctx=ctx),
    )

    assert len(client.calls) == 1
    assert sorted([first.github.request_performed, second.github.request_performed]) == [False, True]
    assert sorted([first.github.cached, second.github.cached]) == [False, True]


def test_rate_status_refresh_floor_cannot_be_configured_below_one_second() -> None:
    with pytest.raises(ValidationError):
        Settings(api_rate_status_min_interval_seconds=0.999)
