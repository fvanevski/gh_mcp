"""Cancellation regression for exact workflow dispatch reservations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.request_governor import GitHubRequestError, GitHubRequestResult
from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools.workflow_dispatch import (
    WorkflowDispatchUncertainError,
    _WORKFLOW_DISPATCH_RESERVATIONS,
    gh_run_workflow_exact,
)


@dataclass
class CancellationClient:
    """Protocol fake that can be cancelled while the governed POST is in flight."""

    read_results: list[Any]
    write_entered: asyncio.Event
    release_write: asyncio.Event
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        del kwargs
        self.calls.append(("read", args))
        result = self.read_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(
        self,
        *args: str,
        **kwargs: Any,
    ) -> GitHubRequestResult[Any]:
        del kwargs
        self.calls.append(("write", args))
        if "--input" in args:
            payload_path = Path(args[args.index("--input") + 1])
            self.payloads.append(json.loads(payload_path.read_text()))
        self.write_entered.set()
        await self.release_write.wait()
        return GitHubRequestResult(value={})

    def clamp_max_results(self, requested: int | None) -> int:
        return requested if requested is not None else 30


def _context(client: CancellationClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(
            allow_write_commands=True,
            allow_workflow_dispatch=True,
        ),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _ref(sha: str) -> dict[str, Any]:
    return {
        "ref": "refs/heads/main",
        "object": {
            "type": "commit",
            "sha": sha,
            "url": f"https://api.github.com/repos/octo/repo/git/commits/{sha}",
        },
    }


def _runs() -> dict[str, Any]:
    return {"total_count": 0, "workflow_runs": []}


async def test_cancelled_inflight_dispatch_leaves_fail_closed_reservation() -> None:
    sha = "f" * 40
    key = ("octo", "repo", 17, sha)
    write_entered = asyncio.Event()
    release_write = asyncio.Event()
    client = CancellationClient(
        read_results=[
            _ref(sha),
            _runs(),
            GitHubRequestError("missing tag", status_code=404),
            [],
            _ref(sha),
            _runs(),
        ],
        write_entered=write_entered,
        release_write=release_write,
    )
    ctx = _context(client)

    task = asyncio.create_task(
        gh_run_workflow_exact(
            "octo",
            "repo",
            17,
            "heads/main",
            sha,
            ctx=ctx,
        )
    )
    await write_entered.wait()
    task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await task

        with pytest.raises(WorkflowDispatchUncertainError, match="unresolved transport outcome"):
            await gh_run_workflow_exact(
                "octo",
                "repo",
                17,
                "heads/main",
                sha,
                ctx=ctx,
            )

        assert sum(kind == "write" for kind, _ in client.calls) == 1
        assert client.payloads == [{"ref": "main", "return_run_details": True}]
    finally:
        _WORKFLOW_DISPATCH_RESERVATIONS.pop(key, None)
        release_write.set()
