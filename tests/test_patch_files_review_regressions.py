"Focused regressions for independent PR #81 review findings on gh_patch_files."

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from mcp_gh_server.patch_models import FilePatch, PatchEdit
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.settings import Settings
from mcp_gh_server.tooling import AppContext
from mcp_gh_server.tools.patch_writes import _materialize_edits, gh_patch_files


@dataclass
class ReviewPatchClient:
    read_results: list[Any] = field(default_factory=list)
    write_results: list[Any] = field(default_factory=list)
    calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append(("read", args, kwargs))
        result = self.read_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_with_metadata(
        self,
        *args: str,
        **kwargs: Any,
    ) -> GitHubRequestResult[Any]:
        self.calls.append(("write", args, kwargs))
        if "--input" in args:
            payload_path = Path(args[args.index("--input") + 1])
            self.payloads.append(json.loads(payload_path.read_text()))
        result = self.write_results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, GitHubRequestResult):
            return result
        return GitHubRequestResult(value=result)


def _context(client: ReviewPatchClient, **settings: Any) -> Any:
    values: dict[str, Any] = {
        "allow_write_commands": True,
        "allow_content_commits": True,
    }
    values.update(settings)
    app = AppContext(
        client=cast(Any, client),
        settings=Settings(**values),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _ref(sha: str) -> dict[str, Any]:
    return {"ref": "refs/heads/main", "object": {"sha": sha}}


def _tree_entry(path: str, sha: str, *, size: int, mode: str = "100644") -> dict[str, Any]:
    return {"path": path, "mode": mode, "type": "blob", "sha": sha, "size": size}


def _tree(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"sha": "b" * 40, "truncated": False, "tree": list(entries)}


def _blob(sha: str, text: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
    }


def _writes(client: ReviewPatchClient, endpoint_suffix: str) -> list[tuple[str, ...]]:
    return [
        args
        for kind, args, _ in client.calls
        if kind == "write" and len(args) > 1 and args[1].endswith(endpoint_suffix)
    ]


async def _patch(
    client: ReviewPatchClient,
    head: str,
    *,
    old_text: str = "alpha",
    new_text: str = "beta",
    path: str = "a.txt",
    ctx: Any | None = None,
) -> Any:
    return await gh_patch_files(
        "octo",
        "repo",
        "main",
        head,
        [FilePatch(path=path, edits=[PatchEdit(old_text=old_text, new_text=new_text)])],
        "patch a.txt",
        ctx=ctx or _context(client),
    )


def test_maximum_edit_count_materializes_in_one_ordered_assembly() -> None:
    original = "\n".join(f"item-{index:04d}" for index in range(1000))
    patch = FilePatch(
        path="a.txt",
        edits=[
            PatchEdit(old_text=f"item-{index:04d}", new_text=f"value-{index:04d}")
            for index in range(1000)
        ],
    )

    materialized, edit_count = _materialize_edits("a.txt", original, patch)

    assert edit_count == 1000
    assert materialized.startswith("value-0000\nvalue-0001")
    assert materialized.endswith("value-0998\nvalue-0999")
    assert "item-" not in materialized


async def test_invalid_patch_path_fails_before_any_repository_call() -> None:
    client = ReviewPatchClient()

    with pytest.raises(ValueError):
        await _patch(
            client,
            "a" * 40,
            old_text="a",
            new_text="b",
            path="../escape.txt",
        )

    assert client.calls == []


async def test_oversized_target_is_rejected_from_tree_metadata_before_blob_fetch() -> None:
    head = "a" * 40
    old_blob = "c" * 40
    client = ReviewPatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": "b" * 40}},
            _tree(_tree_entry("a.txt", old_blob, size=5)),
        ]
    )

    with pytest.raises(ValueError, match="MCP_GH_MAX_FILE_BYTES=4"):
        await _patch(
            client,
            head,
            old_text="a",
            new_text="b",
            ctx=_context(client, max_file_bytes=4),
        )

    assert not any(
        kind == "read" and len(args) > 1 and "/git/blobs/" in args[1]
        for kind, args, _ in client.calls
    )
    assert all(kind == "read" for kind, _, _ in client.calls)


async def test_materialized_file_size_bound_fails_before_git_object_creation() -> None:
    head = "a" * 40
    old_blob = "c" * 40
    original = "abc"
    client = ReviewPatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": "b" * 40}},
            _tree(_tree_entry("a.txt", old_blob, size=len(original.encode()))),
            _blob(old_blob, original),
        ]
    )

    with pytest.raises(ValueError, match=r"patched file .* exceeds MCP_GH_MAX_FILE_BYTES=5"):
        await _patch(
            client,
            head,
            old_text="b",
            new_text="BBBB",
            ctx=_context(client, max_file_bytes=5),
        )

    assert all(kind == "read" for kind, _, _ in client.calls)


async def test_known_cas_failure_after_object_creation_attempts_exactly_one_cas() -> None:
    head = "a" * 40
    old_blob = "c" * 40
    new_blob = "d" * 40
    tree_sha = "e" * 40
    commit_sha = "f" * 40
    original = "alpha"
    cas_failure = GitHubRequestError(
        "ref update rejected",
        retryable=False,
        ambiguous=False,
        metadata=GitHubRequestMetadata(request_id="req-known-failure"),
    )
    client = ReviewPatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": "b" * 40}},
            _tree(_tree_entry("a.txt", old_blob, size=len(original.encode()))),
            _blob(old_blob, original),
            _ref(head),
            _ref(head),
        ],
        write_results=[
            {"sha": new_blob},
            {"sha": tree_sha},
            {"sha": commit_sha},
            cas_failure,
        ],
    )

    result = await _patch(client, head)

    assert result.write_completed is False
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.ref_updated is False
    assert result.observed_head_sha == head
    assert result.readback_attempts == 1
    assert result.request_id == "req-known-failure"
    assert len(_writes(client, "/git/commits")) == 1
    assert len(_writes(client, "graphql")) == 1


async def test_successful_cas_with_exhausted_old_head_reads_remains_unresolved() -> None:
    head = "a" * 40
    old_blob = "c" * 40
    new_blob = "d" * 40
    tree_sha = "e" * 40
    commit_sha = "f" * 40
    original = "alpha"
    client = ReviewPatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": "b" * 40}},
            _tree(_tree_entry("a.txt", old_blob, size=len(original.encode()))),
            _blob(old_blob, original),
            _ref(head),
            _ref(head),
            _ref(head),
            _ref(head),
        ],
        write_results=[
            {"sha": new_blob},
            {"sha": tree_sha},
            {"sha": commit_sha},
            GitHubRequestResult(
                value={"data": {"updateRefs": {"clientMutationId": None}}},
                metadata=GitHubRequestMetadata(request_id="req-stale-readback"),
            ),
        ],
    )

    result = await _patch(client, head)

    assert result.write_completed is True
    assert result.state_matches_requested is None
    assert result.ref_updated is None
    assert result.observed_head_sha == head
    assert result.readback_attempts == 3
    assert len(_writes(client, "graphql")) == 1


async def test_third_party_head_after_cas_is_conclusive_mismatch_without_replay() -> None:
    head = "a" * 40
    third_party_head = "9" * 40
    old_blob = "c" * 40
    new_blob = "d" * 40
    tree_sha = "e" * 40
    commit_sha = "f" * 40
    original = "alpha"
    client = ReviewPatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": "b" * 40}},
            _tree(_tree_entry("a.txt", old_blob, size=len(original.encode()))),
            _blob(old_blob, original),
            _ref(head),
            _ref(third_party_head),
        ],
        write_results=[
            {"sha": new_blob},
            {"sha": tree_sha},
            {"sha": commit_sha},
            GitHubRequestResult(
                value={"data": {"updateRefs": {"clientMutationId": None}}},
                metadata=GitHubRequestMetadata(request_id="req-third-party"),
            ),
        ],
    )

    result = await _patch(client, head)

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.ref_updated is False
    assert result.observed_head_sha == third_party_head
    assert result.readback_attempts == 1
    assert len(_writes(client, "graphql")) == 1
