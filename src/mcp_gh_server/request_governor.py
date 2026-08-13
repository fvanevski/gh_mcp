"""Shared serialization, pacing, retry, and rate-limit governance for GitHub requests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from time import monotonic, time
from typing import Literal, TypeVar

T = TypeVar("T")
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
RateBlockReason = Literal["retry_after", "primary_reset", "fallback"]


class GitHubRequestKind(StrEnum):
    """Internal request classification used by the governor."""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class GitHubRequestPolicy:
    """Retry and mutation policy for one governed ``gh`` execution."""

    kind: GitHubRequestKind
    retry_safe: bool


READ_REQUEST = GitHubRequestPolicy(kind=GitHubRequestKind.READ, retry_safe=True)
WRITE_REQUEST = GitHubRequestPolicy(kind=GitHubRequestKind.WRITE, retry_safe=False)


@dataclass(frozen=True, slots=True)
class GitHubRequestMetadata:
    """Transport metadata available to higher-level read/write result contracts."""

    request_id: str | None = None
    warning: str | None = None
    attempts: int = 1
    retry_after_seconds: float | None = None
    rate_limit_reset_epoch: int | None = None
    rate_limit_remaining: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    rate_limit_resource: str | None = None
    rate_limit_limit: int | None = None
    rate_limit_used: int | None = None


@dataclass(frozen=True, slots=True)
class GitHubRequestResult[T]:
    """One governed result plus request metadata."""

    value: T
    metadata: GitHubRequestMetadata = field(default_factory=GitHubRequestMetadata)


@dataclass(frozen=True, slots=True)
class GitHubGovernorSnapshot:
    """Read-only point-in-time view of local rate-limit and write-pacing policy state."""

    observed_at_epoch: float
    reads_blocked: bool
    writes_blocked: bool
    writes_delayed: bool
    write_delay_seconds: float
    blocked_until_epoch: float | None
    retry_after_seconds: float | None
    block_reason: RateBlockReason | None
    last_rate_event_at_epoch: float | None
    last_rate_request_id: str | None
    last_rate_warning: str | None


class GitHubRequestError(RuntimeError):
    """Structured command failure consumed by ``GitHubRequestGovernor``."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        ambiguous: bool = False,
        rate_limited: bool = False,
        governor_blocked: bool = False,
        metadata: GitHubRequestMetadata | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.rate_limited = rate_limited
        self.governor_blocked = governor_blocked
        self.metadata = metadata or GitHubRequestMetadata()


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class GitHubRequestGovernor:
    """Serialize GitHub requests and apply conservative retry/rate-limit policy."""

    def __init__(
        self,
        *,
        write_spacing_seconds: float = 1.0,
        max_read_attempts: int = 3,
        backoff_base_seconds: float = 0.25,
        backoff_max_seconds: float = 2.0,
        rate_limit_fallback_seconds: float = 60.0,
        monotonic_clock: Clock = monotonic,
        wall_clock: Clock = time,
        sleep: Sleep = _default_sleep,
    ) -> None:
        if write_spacing_seconds < 1.0:
            raise ValueError("write_spacing_seconds must be at least 1.0")
        if max_read_attempts < 1:
            raise ValueError("max_read_attempts must be at least 1")
        if backoff_base_seconds < 0 or backoff_max_seconds < 0:
            raise ValueError("backoff delays must be non-negative")
        if backoff_base_seconds > backoff_max_seconds:
            raise ValueError("backoff_base_seconds cannot exceed backoff_max_seconds")
        if rate_limit_fallback_seconds <= 0:
            raise ValueError("rate_limit_fallback_seconds must be positive")

        self._write_spacing_seconds = write_spacing_seconds
        self._max_read_attempts = max_read_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._rate_limit_fallback_seconds = rate_limit_fallback_seconds
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._serial_lock = asyncio.Lock()
        self._last_write_finished_at: float | None = None
        self._blocked_until_wall: float | None = None
        self._blocked_metadata: GitHubRequestMetadata | None = None
        self._blocked_reason: RateBlockReason | None = None
        self._last_rate_event_at_wall: float | None = None
        self._last_rate_request_id: str | None = None
        self._last_rate_warning: str | None

    async def execute(
        self,
        policy: GitHubRequestPolicy,
        operation: Callable[[], Awaitable[GitHubRequestResult[T]]],
    ) -> GitHubRequestResult[T]:
        """Run one request under serialization, pacing, retry, and cooldown policy."""

        if policy.kind is GitHubRequestKind.WRITE and policy.retry_safe:
            raise ValueError("write requests cannot be marked retry-safe")

        async with self._serial_lock:
            self._enforce_rate_limit_pause()
            attempts = 0
            while True:
                attempts += 1
                if policy.kind is GitHubRequestKind.WRITE:
                    await self._pace_write()

                try:
                    result = await operation()
                except GitHubRequestError as exc:
                    if policy.kind is GitHubRequestKind.WRITE:
                        self._last_write_finished_at = self._monotonic_clock()
                    if exc.rate_limited:
                        self._record_rate_limit(exc.metadata)
                        raise
                    if not self._should_retry(policy, exc, attempts):
                        raise
                    await self._sleep(self._backoff_delay(attempts))
                    continue
                except BaseException:
                    if policy.kind is GitHubRequestKind.WRITE:
                        self._last_write_finished_at = self._monotonic_clock()
                    raise

                if policy.kind is GitHubRequestKind.WRITE:
                    self._last_write_finished_at = self._monotonic_clock()

                metadata = replace(result.metadata, attempts=attempts)
                warning_parts: list[str] = []
                if metadata.warning:
                    warning_parts.append(metadata.warning)
                if attempts > 1:
                    warning_parts.append(
                        "GitHub read succeeded after "
                        f"{attempts} attempts following transient failures."
                    )
                rate_warning = self._record_success_rate_limit(metadata)
                if rate_warning:
                    warning_parts.append(rate_warning)
                if warning_parts:
                    metadata = replace(metadata, warning=" ".join(warning_parts))
                return replace(result, metadata=metadata)

    def rate_status(self) -> GitHubGovernorSnapshot:
        """Return local governor status without issuing or bypassing a GitHub request."""

        now_wall = self._wall_clock()
        self._expire_rate_limit_state(now_wall)
        blocked = self._blocked_until_wall is not None
        retry_after = (
            max(self._blocked_until_wall - now_wall, 0.0)
            if self._blocked_until_wall is not None
            else None
        )
        write_delay = self._write_pacing_delay()
        return GitHubGovernorSnapshot(
            observed_at_epoch=now_wall,
            reads_blocked=blocked,
            writes_blocked=blocked,
            writes_delayed=write_delay > 0,
            write_delay_seconds=write_delay,
            blocked_until_epoch=self._blocked_until_wall,
            retry_after_seconds=retry_after,
            block_reason=self._blocked_reason,
            last_rate_event_at_epoch=self._last_rate_event_at_wall,
            last_rate_request_id=self._last_rate_request_id,
            last_rate_warning=self._last_rate_warning,
        )

    def _should_retry(
        self,
        policy: GitHubRequestPolicy,
        error: GitHubRequestError,
        attempts: int,
    ) -> bool:
        return (
            policy.kind is GitHubRequestKind.READ
            and policy.retry_safe
            and error.retryable
            and not error.rate_limited
            and attempts < self._max_read_attempts
        )

    def _backoff_delay(self, attempts: int) -> float:
        delay = float(self._backoff_base_seconds * (2 ** max(attempts - 1, 0)))
        return min(delay, self._backoff_max_seconds)

    def _write_pacing_delay(self) -> float:
        if self._last_write_finished_at is None:
            return 0.0
        elapsed = self._monotonic_clock() - self._last_write_finished_at
        return max(self._write_spacing_seconds - elapsed, 0.0)

    async def _pace_write(self) -> None:
        remaining = self._write_pacing_delay()
        if remaining > 0:
            await self._sleep(remaining)

    def _record_rate_limit(
        self,
        metadata: GitHubRequestMetadata,
        *,
        warning: str | None = None,
    ) -> None:
        deadline, reason = self._rate_limit_deadline(metadata)
        blocked_metadata = metadata
        if deadline is None:
            deadline = self._wall_clock() + self._rate_limit_fallback_seconds
            reason = "fallback"
            blocked_metadata = replace(
                metadata,
                retry_after_seconds=self._rate_limit_fallback_seconds,
            )
        effective_warning = warning or metadata.warning or self._rate_limit_warning(reason)
        blocked_metadata = replace(blocked_metadata, warning=effective_warning)
        self._last_rate_event_at_wall = self._wall_clock()
        self._last_rate_request_id = metadata.request_id
        self._last_rate_warning = effective_warning
        if self._blocked_until_wall is None or deadline > self._blocked_until_wall:
            self._blocked_until_wall = deadline
            self._blocked_metadata = blocked_metadata
            self._blocked_reason = reason

    def _record_success_rate_limit(self, metadata: GitHubRequestMetadata) -> str | None:
        if metadata.rate_limit_remaining != 0 or metadata.rate_limit_reset_epoch is None:
            return None
        deadline = float(metadata.rate_limit_reset_epoch)
        if deadline <= self._wall_clock():
            return None
        warning = (
            "GitHub primary rate limit is exhausted; further requests are blocked until "
            "the reported reset time."
        )
        self._record_rate_limit(metadata, warning=warning)
        return warning

    def _rate_limit_deadline(
        self,
        metadata: GitHubRequestMetadata,
    ) -> tuple[float | None, RateBlockReason | None]:
        if metadata.retry_after_seconds is not None:
            return (
                self._wall_clock() + max(metadata.retry_after_seconds, 0.0),
                "retry_after",
            )
        if metadata.rate_limit_remaining == 0 and metadata.rate_limit_reset_epoch is not None:
            return float(metadata.rate_limit_reset_epoch), "primary_reset"
        return None, None

    @staticmethod
    def _rate_limit_warning(reason: RateBlockReason | None) -> str:
        if reason == "retry_after":
            return (
                "GitHub rate-limit or abuse throttling supplied Retry-After; local governor "
                "cooldown is active."
            )
        if reason == "primary_reset":
            return (
                "GitHub primary rate limit is exhausted; local governor blocks requests until "
                "the reported reset time."
            )
        return (
            "GitHub rate-limit or abuse throttling supplied no usable Retry-After/reset; "
            "local governor fallback cooldown is active."
        )

    def _expire_rate_limit_state(self, now: float) -> None:
        if self._blocked_until_wall is None or now < self._blocked_until_wall:
            return
        self._blocked_until_wall = None
        self._blocked_metadata = None
        self._blocked_reason = None

    def _enforce_rate_limit_pause(self) -> None:
        now = self._wall_clock()
        self._expire_rate_limit_state(now)
        if self._blocked_until_wall is None:
            return

        remaining = self._blocked_until_wall - now
        metadata = self._blocked_metadata or GitHubRequestMetadata()
        metadata = replace(metadata, retry_after_seconds=remaining)
        raise GitHubRequestError(
            "GitHub request governor is paused by a prior rate-limit response; "
            f"retry after at least {remaining:.3f}s.",
            rate_limited=True,
            governor_blocked=True,
            metadata=metadata,
        )
