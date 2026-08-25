"Regression coverage for issue #80 exact-context repository file patches."

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.patch_models import FilePatch, PatchEdit
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.settings import Settings
from mcp_gh_server.tooling import AppContext
from mcp_gh_server.tools.patch_writes import gh_patch_files


@dataclass
class PatchClient:
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


def _context(client: PatchClient, **settings: Any) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(
            allow_write_commands=True,
            allow_content_commits=True,
            **settings,
        ),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _ref(sha: str) -> dict[str, Any]:
    return {"ref": "refs/heads/main", "object": {"sha": sha}}


def _tree(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"sha": "b" * 40, "truncated": False, "tree": list(entries)}


def _entry(path: str, sha: str, *, mode: str = "100644", kind: str = "blob") -> dict[str, Any]:
    return {"path": path, "mode": mode, "type": kind, "sha": sha}


def _blob(sha: str, text: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
    }


def _success_client(
    original: str,
    *,
    mode: str = "100644",
    readbacks: list[Any] | None = None,
    cas_result: Any | None = None,
) -> tuple[PatchClient, str, str, str]:
    head = "a" * 40
    base_tree = "b" * 40
    old_blob = "c" * 40
    new_blob = "d" * 40
    tree_sha = "e" * 40
    commit_sha = "f" * 40
    if cas_result is None:
        cas_result = GitHubRequestResult(
            value={"data": {"updateRefs": {"clientMutationId": None}}},
            metadata=GitHubRequestMetadata(request_id="req-patch"),
        )
    if readbacks is None:
        readbacks = [_ref(commit_sha)]
    client = PatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": base_tree}},
            _tree(_entry("a.txt", old_blob, mode=mode)),
            _blob(old_blob, original),
            _ref(head),
            *readbacks,
        ],
        write_results=[
            {"sha": new_blob},
            {"sha": tree_sha},
            {
                "sha": commit_sha,
                "html_url": f"https://github.com/octo/repo/commit/{commit_sha}",
            },
            cas_result,
        ],
    )
    return client, head, old_blob, commit_sha


async def _patch(
    client: PatchClient,
    head: str,
    edits: list[PatchEdit],
) -> Any:
    return await gh_patch_files(
        "octo",
        "repo",
        "main",
        head,
        [FilePatch(path="a.txt", edits=edits)],
        "patch a.txt",
        ctx=_context(client),
    )


def _write_payload(client: PatchClient, endpoint_suffix: str) -> dict[str, Any]:
    for (kind, args, _), payload in zip(
        [call for call in client.calls if call[0] == "write"],
        client.payloads,
        strict=False,
    ):
        if kind == "write" and len(args) > 1 and args[1].endswith(endpoint_suffix):
            return payload
    raise AssertionError(f"missing write payload for {endpoint_suffix}")


async def test_unique_single_line_replacement_and_evidence() -> None:
    client, head, old_blob, commit_sha = _success_client("alpha\nbeta\ngamma\n")

    result = await _patch(
        client,
        head,
        [PatchEdit(old_text="beta", new_text="BETA")],
    )

    assert result.state_matches_requested is True
    assert result.ref_updated is True
    assert result.commit_sha == commit_sha
    assert result.changed_file_count == 1
    assert result.applied_edit_count == 1
    assert result.changed_paths == ["a.txt"]
    assert result.files[0].before_blob_sha == old_blob
    assert result.files[0].after_blob_sha == "d" * 40
    assert result.files[0].mode == "100644"
    blob_payload = _write_payload(client, "/git/blobs")
    assert blob_payload["content"] == "alpha\nBETA\ngamma\n"


async def test_multiline_replacement_and_deletion() -> None:
    client, head, _, _ = _success_client("start\none\ntwo\nend\n")

    await _patch(
        client,
        head,
        [
            PatchEdit(old_text="one\ntwo\n", new_text="ONE\n"),
            PatchEdit(old_text="end", new_text=""),
        ],
    )

    blob_payload = _write_payload(client, "/git/blobs")
    assert blob_payload["content"] == "start\nONE\n"


async def test_multiple_edits_are_resolved_against_original_not_incrementally() -> None:
    client, head, _, _ = _success_client("alpha beta gamma")

    await _patch(
        client,
        head,
        [
            PatchEdit(old_text="alpha", new_text="beta"),
            PatchEdit(old_text="beta", new_text="BETA"),
        ],
    )

    blob_payload = _write_payload(client, "/git/blobs")
    assert blob_payload["content"] == "beta BETA gamma"


@pytest.mark.parametrize(
    ("original", "old_text", "message"),
    [
        ("alpha beta", "missing", "was not found"),
        ("alpha beta alpha", "alpha", "occurs 2 times"),
    ],
)
async def test_missing_or_nonunique_context_rejects_before_git_object_creation(
    original: str,
    old_text: str,
    message: str,
) -> None:
    head = "a" * 40
    old_blob = "c" * 40
    client = PatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": "b" * 40}},
            _tree(_entry("a.txt", old_blob)),
            _blob(old_blob, original),
        ]
    )

    with pytest.raises(ValueError, match=message):
        await _patch(client, head, [PatchEdit(old_text=old_text, new_text="x")])

    assert all(kind == "read" for kind, _, _ in client.calls)


async def test_overlapping_source_spans_reject_before_git_object_creation() -> None:
    head = "a" * 40
    old_blob = "c" * 40
    client = PatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": "b" * 40}},
            _tree(_entry("a.txt", old_blob)),
            _blob(old_blob, "abcdef"),
        ]
    )

    with pytest.raises(ValueError, match="overlapping"):
        await _patch(
            client,
            head,
            [
                PatchEdit(old_text="abcd", new_text="A"),
                PatchEdit(old_text="cdef", new_text="B"),
            ],
        )

    assert all(kind == "read" for kind, _, _ in client.calls)


async def test_stale_head_rejects_before_patch_reads_or_git_object_creation() -> None:
    expected = "a" * 40
    actual = "9" * 40
    client = PatchClient(read_results=[_ref(actual)])

    with pytest.raises(RuntimeError, match="head mismatch"):
        await _patch(
            client,
            expected,
            [PatchEdit(old_text="alpha", new_text="beta")],
        )

    assert len(client.calls) == 1
    assert client.calls[0][0] == "read"


async def test_head_change_during_validation_rejects_before_git_object_creation() -> None:
    head = "a" * 40
    moved = "9" * 40
    old_blob = "c" * 40
    client = PatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": "b" * 40}},
            _tree(_entry("a.txt", old_blob)),
            _blob(old_blob, "alpha"),
            _ref(moved),
        ]
    )

    with pytest.raises(RuntimeError, match="changed during exact-context patch validation"):
        await _patch(
            client,
            head,
            [PatchEdit(old_text="alpha", new_text="beta")],
        )

    assert all(kind == "read" for kind, _, _ in client.calls)


async def test_original_executable_mode_is_preserved_in_tree_entry() -> None:
    client, head, _, _ = _success_client("#!/bin/sh\necho old\n", mode="100755")

    result = await _patch(
        client,
        head,
        [PatchEdit(old_text="old", new_text="new")],
    )

    tree_payload = _write_payload(client, "/git/trees")
    assert tree_payload["tree"][0]["mode"] == "100755"
    assert result.files[0].mode == "100755"


async def test_symlink_target_fails_closed_before_blob_read_or_write() -> None:
    head = "a" * 40
    old_blob = "c" * 40
    client = PatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": "b" * 40}},
            _tree(_entry("a.txt", old_blob, mode="120000")),
        ]
    )

    with pytest.raises(ValueError, match="unsupported Git mode"):
        await _patch(
            client,
            head,
            [PatchEdit(old_text="alpha", new_text="beta")],
        )

    assert all(kind == "read" for kind, _, _ in client.calls)


async def test_multi_file_patch_uses_one_commit_and_one_ref_cas() -> None:
    head = "a" * 40
    base_tree = "b" * 40
    old_a, old_b = "c" * 40, "d" * 40
    new_a, new_b = "e" * 40, "1" * 40
    tree_sha, commit_sha = "2" * 40, "3" * 40
    client = PatchClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": base_tree}},
            _tree(_entry("a.txt", old_a), _entry("b.txt", old_b)),
            _blob(old_a, "alpha"),
            _blob(old_b, "bravo"),
            _ref(head),
            _ref(commit_sha),
        ],
        write_results=[
            {"sha": new_a},
            {"sha": new_b},
            {"sha": tree_sha},
            {"sha": commit_sha},
            {"data": {"updateRefs": {"clientMutationId": None}}},
        ],
    )

    result = await gh_patch_files(
        "octo",
        "repo",
        "main",
        head,
        [
            FilePatch(path="a.txt", edits=[PatchEdit(old_text="alpha", new_text="ALPHA")]),
            FilePatch(path="b.txt", edits=[PatchEdit(old_text="bravo", new_text="BRAVO")]),
        ],
        "patch two files",
        ctx=_context(client),
    )

    assert result.changed_file_count == 2
    assert result.applied_edit_count == 2
    graphql_writes = [
        args
        for kind, args, _ in client.calls
        if kind == "write" and args[:2] == ("api", "graphql")
    ]
    commit_writes = [
        args
        for kind, args, _ in client.calls
        if kind == "write" and len(args) > 1 and args[1].endswith("/git/commits")
    ]
    assert len(graphql_writes) == 1
    assert len(commit_writes) == 1
    cas_payload = next(
        payload
        for payload in client.payloads
        if "refUpdates" in payload.get("variables", {}).get("input", {})
    )
    assert cas_payload["variables"]["input"]["refUpdates"] == [
        {
            "name": "refs/heads/main",
            "beforeOid": head,
            "afterOid": commit_sha,
            "force": False,
        }
    ]


async def test_ambiguous_cas_reuses_bounded_reconciliation_without_replay() -> None:
    head = "a" * 40
    commit_sha = "f" * 40
    ambiguous = GitHubRequestError(
        "transport reset",
        retryable=True,
        ambiguous=True,
        metadata=GitHubRequestMetadata(request_id="req-ambiguous"),
    )
    client, _, _, _ = _success_client(
        "alpha",
        readbacks=[_ref(head), _ref(commit_sha)],
        cas_result=ambiguous,
    )

    result = await _patch(
        client,
        head,
        [PatchEdit(old_text="alpha", new_text="beta")],
    )

    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.ref_updated is True
    assert result.readback_attempts == 2
    assert result.request_id == "req-ambiguous"
    graphql_writes = [
        args
        for kind, args, _ in client.calls
        if kind == "write" and args[:2] == ("api", "graphql")
    ]
    assert len(graphql_writes) == 1
