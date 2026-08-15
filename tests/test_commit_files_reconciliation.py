"""Regression coverage for issue #73 exact-ref reconciliation after content CAS."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcp_gh_server.git_write_models import CommitFilesResult
from mcp_gh_server.models import CommitFile
from mcp_gh_server.request_governor import (
    GitHubRequestError,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from mcp_gh_server.settings import Settings
from mcp_gh_server.tooling import AppContext
from mcp_gh_server.tools.content_writes import gh_commit_files


@dataclass
class ReconciliationClient:
    """Queue exact reads/writes and capture JSON mutation payloads."""

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


def _context(client: ReconciliationClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(allow_write_commands=True, allow_content_commits=True),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _ref(sha: str) -> dict[str, Any]:
    return {"ref": "refs/heads/main", "object": {"sha": sha}}


def _client(
    readbacks: list[Any],
    *,
    cas_result: Any | None = None,
) -> tuple[ReconciliationClient, str, str]:
    head = "a" * 40
    base_tree = "b" * 40
    blob_sha = "c" * 40
    tree_sha = "d" * 40
    commit_sha = "e" * 40
    if cas_result is None:
        cas_result = GitHubRequestResult(
            value={"data": {"updateRefs": {"clientMutationId": None}}},
            metadata=GitHubRequestMetadata(request_id="req-cas"),
        )
    client = ReconciliationClient(
        read_results=[
            _ref(head),
            {"node_id": "R_repo"},
            {"tree": {"sha": base_tree}},
            *readbacks,
        ],
        write_results=[
            {"sha": blob_sha},
            {"sha": tree_sha},
            {
                "sha": commit_sha,
                "html_url": f"https://github.com/octo/repo/commit/{commit_sha}",
            },
            cas_result,
        ],
    )
    return client, head, commit_sha


async def _commit(client: ReconciliationClient, head: str) -> CommitFilesResult:
    return await gh_commit_files(
        "octo",
        "repo",
        "main",
        head,
        [CommitFile(path="a.txt", content="replacement\n")],
        "update a.txt",
        ctx=_context(client),
    )


def _assert_single_ref_cas(
    client: ReconciliationClient,
    *,
    previous_head_sha: str,
    commit_sha: str,
) -> None:
    cas_payloads = [
        payload
        for payload in client.payloads
        if isinstance(payload.get("variables"), dict)
        and "refUpdates" in payload["variables"].get("input", {})
    ]
    assert len(cas_payloads) == 1
    update = cas_payloads[0]["variables"]["input"]["refUpdates"]
    assert update == [
        {
            "name": "refs/heads/main",
            "beforeOid": previous_head_sha,
            "afterOid": commit_sha,
            "force": False,
        }
    ]
    graphql_writes = [
        args for kind, args, _ in client.calls if kind == "write" and args[:2] == ("api", "graphql")
    ]
    assert len(graphql_writes) == 1


def _assert_ref_read_count(client: ReconciliationClient, readback_attempts: int) -> None:
    exact_ref_reads = [
        args
        for kind, args, _ in client.calls
        if kind == "read" and args[:2] == ("api", "repos/octo/repo/git/ref/heads/main")
    ]
    assert len(exact_ref_reads) == 1 + readback_attempts


async def test_immediate_new_head_is_verified_success() -> None:
    client, head, commit_sha = _client([_ref("e" * 40)])

    result = await _commit(client, head)

    assert result.write_completed is True
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.ref_updated is True
    assert result.observed_head_sha == commit_sha
    assert result.readback_attempts == 1
    assert result.files_committed == 1
    _assert_single_ref_cas(client, previous_head_sha=head, commit_sha=commit_sha)
    _assert_ref_read_count(client, 1)


async def test_old_head_then_new_head_reconciles_without_replaying_cas() -> None:
    head = "a" * 40
    client, _, commit_sha = _client([_ref(head), _ref("e" * 40)])

    result = await _commit(client, head)

    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.ref_updated is True
    assert result.observed_head_sha == commit_sha
    assert result.readback_attempts == 2
    _assert_single_ref_cas(client, previous_head_sha=head, commit_sha=commit_sha)
    _assert_ref_read_count(client, 2)


async def test_old_head_twice_then_new_head_reconciles_on_final_attempt() -> None:
    head = "a" * 40
    client, _, commit_sha = _client([_ref(head), _ref(head), _ref("e" * 40)])

    result = await _commit(client, head)

    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.ref_updated is True
    assert result.observed_head_sha == commit_sha
    assert result.readback_attempts == 3
    _assert_single_ref_cas(client, previous_head_sha=head, commit_sha=commit_sha)
    _assert_ref_read_count(client, 3)


async def test_old_head_through_bound_is_unresolved_not_false_failure() -> None:
    head = "a" * 40
    client, _, commit_sha = _client([_ref(head), _ref(head), _ref(head)])

    result = await _commit(client, head)

    assert result.write_completed is True
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.ref_updated is None
    assert result.observed_head_sha == head
    assert result.readback_attempts == 3
    assert result.files_committed == 0
    assert "unresolved" in result.message
    assert "not installed" not in result.message
    assert result.warning is not None
    assert "unresolved, not disproven" in result.warning
    _assert_single_ref_cas(client, previous_head_sha=head, commit_sha=commit_sha)
    _assert_ref_read_count(client, 3)


async def test_third_party_head_is_conclusive_mismatch_with_observed_sha() -> None:
    head = "a" * 40
    third_party = "f" * 40
    client, _, commit_sha = _client([_ref(head), _ref(third_party)])

    result = await _commit(client, head)

    assert result.readback_completed is True
    assert result.state_matches_requested is False
    assert result.ref_updated is False
    assert result.observed_head_sha == third_party
    assert result.readback_attempts == 2
    assert third_party in result.message
    assert result.warning is not None
    assert "conclusive semantic mismatch" in result.warning
    _assert_single_ref_cas(client, previous_head_sha=head, commit_sha=commit_sha)
    _assert_ref_read_count(client, 2)


async def test_readback_failure_after_old_head_is_unresolved() -> None:
    head = "a" * 40
    client, _, commit_sha = _client([_ref(head), RuntimeError("readback unavailable")])

    result = await _commit(client, head)

    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.ref_updated is None
    assert result.observed_head_sha == head
    assert result.readback_attempts == 2
    assert result.warning is not None
    assert "remains unverified" in result.warning
    _assert_single_ref_cas(client, previous_head_sha=head, commit_sha=commit_sha)
    _assert_ref_read_count(client, 2)


async def test_malformed_ref_after_old_head_fails_closed() -> None:
    head = "a" * 40
    client, _, commit_sha = _client([_ref(head), {"object": {"sha": "not-a-sha"}}])

    result = await _commit(client, head)

    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.ref_updated is None
    assert result.observed_head_sha == head
    assert result.readback_attempts == 2
    _assert_single_ref_cas(client, previous_head_sha=head, commit_sha=commit_sha)
    _assert_ref_read_count(client, 2)


async def test_ambiguous_cas_old_then_new_preserves_unknown_transport_state() -> None:
    head = "a" * 40
    ambiguous = GitHubRequestError(
        "transport reset",
        retryable=True,
        ambiguous=True,
        metadata=GitHubRequestMetadata(request_id="req-ambiguous"),
    )
    client, _, commit_sha = _client([_ref(head), _ref("e" * 40)], cas_result=ambiguous)

    result = await _commit(client, head)

    assert result.write_completed is None
    assert result.readback_completed is True
    assert result.state_matches_requested is True
    assert result.ref_updated is True
    assert result.observed_head_sha == commit_sha
    assert result.readback_attempts == 2
    assert result.request_id == "req-ambiguous"
    assert result.warning is not None
    assert "transport outcome is unknown" in result.warning
    assert "Do not retry the mutation" in result.warning
    _assert_single_ref_cas(client, previous_head_sha=head, commit_sha=commit_sha)
    _assert_ref_read_count(client, 2)


async def test_ambiguous_cas_exhausted_old_head_remains_unresolved_without_replay() -> None:
    head = "a" * 40
    ambiguous = GitHubRequestError(
        "transport reset",
        retryable=True,
        ambiguous=True,
        metadata=GitHubRequestMetadata(request_id="req-ambiguous-old"),
    )
    client, _, commit_sha = _client(
        [_ref(head), _ref(head), _ref(head)],
        cas_result=ambiguous,
    )

    result = await _commit(client, head)

    assert result.write_completed is None
    assert result.readback_completed is False
    assert result.state_matches_requested is None
    assert result.ref_updated is None
    assert result.observed_head_sha == head
    assert result.readback_attempts == 3
    assert result.request_id == "req-ambiguous-old"
    _assert_single_ref_cas(client, previous_head_sha=head, commit_sha=commit_sha)
    _assert_ref_read_count(client, 3)
