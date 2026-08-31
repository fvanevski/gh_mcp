"""Regression tests for bounded exact-commit repository-tree discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from mcp_gh_server.server import AppContext, gh_list_repository_tree, mcp
from mcp_gh_server.settings import Settings


@dataclass
class FakeGhClient:
    """Record governed read requests and return queued GitHub payloads."""

    results: list[Any]
    default_max_results: int = 30
    hard_max_results: int = 100
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def clamp_max_results(self, requested: int | None) -> int:
        value = self.default_max_results if requested is None else requested
        if value < 0:
            raise ValueError("per_page must be zero or greater")
        return min(value, self.hard_max_results)


def _context(client: FakeGhClient) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=Settings(),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _commit(commit_sha: str, tree_sha: str) -> dict[str, Any]:
    return {"sha": commit_sha, "tree": {"sha": tree_sha}}


def _entry(
    path: str,
    *,
    type: str,
    mode: str,
    sha: str,
    size: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"path": path, "type": type, "mode": mode, "sha": sha}
    if size is not None:
        result["size"] = size
    return result


def _tree(
    tree_sha: str,
    entries: list[dict[str, Any]],
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    return {"sha": tree_sha, "tree": entries, "truncated": truncated}


async def test_root_non_recursive_listing_preserves_exact_git_evidence() -> None:
    commit_sha = "a" * 40
    root_sha = "b" * 40
    client = FakeGhClient(
        [
            _commit(commit_sha, root_sha),
            _tree(
                root_sha,
                [
                    _entry("README.md", type="blob", mode="100644", sha="c" * 40, size=42),
                    _entry("src", type="tree", mode="040000", sha="d" * 40),
                    _entry("vendor", type="commit", mode="160000", sha="e" * 40),
                ],
            ),
        ]
    )

    result = await gh_list_repository_tree(
        "octo",
        "repo",
        commit_sha,
        path="",
        ctx=_context(client),
    )

    assert result.commit_sha == commit_sha
    assert result.root_tree_sha == root_sha
    assert result.directory_tree_sha == root_sha
    assert result.path == ""
    assert result.recursive is False
    assert result.entries_returned == 3
    assert result.truncated is False
    assert result.evidence_complete is True
    assert result.warning is None
    assert [(item.path, item.name, item.type, item.mode, item.sha, item.size) for item in result.entries] == [
        ("README.md", "README.md", "blob", "100644", "c" * 40, 42),
        ("src", "src", "tree", "040000", "d" * 40, None),
        ("vendor", "vendor", "commit", "160000", "e" * 40, None),
    ]
    assert client.calls == [
        (("api", f"repos/octo/repo/git/commits/{commit_sha}", "-X", "GET"), {}),
        (("api", f"repos/octo/repo/git/trees/{root_sha}", "-X", "GET"), {}),
    ]


async def test_nested_directory_listing_prefixes_repository_relative_paths() -> None:
    commit_sha = "1" * 40
    root_sha = "2" * 40
    src_sha = "3" * 40
    package_sha = "4" * 40
    client = FakeGhClient(
        [
            _commit(commit_sha, root_sha),
            _tree(root_sha, [_entry("src", type="tree", mode="040000", sha=src_sha)]),
            _tree(src_sha, [_entry("package", type="tree", mode="040000", sha=package_sha)]),
            _tree(
                package_sha,
                [
                    _entry("module.py", type="blob", mode="100644", sha="5" * 40, size=12),
                    _entry("nested", type="tree", mode="040000", sha="6" * 40),
                ],
            ),
        ]
    )

    result = await gh_list_repository_tree(
        "octo",
        "repo",
        commit_sha,
        path="src/package",
        ctx=_context(client),
    )

    assert result.path == "src/package"
    assert result.directory_tree_sha == package_sha
    assert [item.path for item in result.entries] == [
        "src/package/module.py",
        "src/package/nested",
    ]


async def test_recursive_listing_returns_descendants_under_requested_directory() -> None:
    commit_sha = "a" * 40
    root_sha = "b" * 40
    docs_sha = "c" * 40
    client = FakeGhClient(
        [
            _commit(commit_sha, root_sha),
            _tree(root_sha, [_entry("docs", type="tree", mode="040000", sha=docs_sha)]),
            _tree(
                docs_sha,
                [
                    _entry("guide.md", type="blob", mode="100644", sha="d" * 40, size=10),
                    _entry("api", type="tree", mode="040000", sha="e" * 40),
                    _entry("api/index.md", type="blob", mode="100644", sha="f" * 40, size=20),
                ],
            ),
        ]
    )

    result = await gh_list_repository_tree(
        "octo",
        "repo",
        commit_sha,
        path="docs",
        recursive=True,
        ctx=_context(client),
    )

    assert result.recursive is True
    assert [item.path for item in result.entries] == [
        "docs/guide.md",
        "docs/api",
        "docs/api/index.md",
    ]
    assert client.calls[-1][0] == (
        "api",
        f"repos/octo/repo/git/trees/{docs_sha}",
        "-X",
        "GET",
        "-f",
        "recursive=1",
    )


@pytest.mark.parametrize(
    "path",
    [
        "/src",
        "src/../docs",
        "src//docs",
        "src/./docs",
        "src\\docs",
        "src\x00docs",
        ".git",
        "src/.git/config",
    ],
)
async def test_unsafe_directory_paths_fail_before_github(path: str) -> None:
    client = FakeGhClient([])

    with pytest.raises(ValueError, match="directory path"):
        await gh_list_repository_tree(
            "octo",
            "repo",
            "a" * 40,
            path=path,
            ctx=_context(client),
        )

    assert client.calls == []


@pytest.mark.parametrize("commit_sha", ["a" * 39, "deadbeef", "main", "g" * 40])
async def test_non_exact_commit_sha_fails_before_github(commit_sha: str) -> None:
    client = FakeGhClient([])

    with pytest.raises(ValidationError):
        await gh_list_repository_tree(
            "octo",
            "repo",
            commit_sha,
            ctx=_context(client),
        )

    assert client.calls == []


async def test_missing_directory_and_file_as_directory_are_distinct() -> None:
    commit_sha = "a" * 40
    root_sha = "b" * 40
    missing_client = FakeGhClient(
        [
            _commit(commit_sha, root_sha),
            _tree(root_sha, [_entry("src", type="tree", mode="040000", sha="c" * 40)]),
        ]
    )

    with pytest.raises(ValueError, match="directory component not found: 'docs'"):
        await gh_list_repository_tree(
            "octo",
            "repo",
            commit_sha,
            path="docs",
            ctx=_context(missing_client),
        )

    blob_client = FakeGhClient(
        [
            _commit(commit_sha, root_sha),
            _tree(
                root_sha,
                [_entry("README.md", type="blob", mode="100644", sha="d" * 40, size=10)],
            ),
        ]
    )
    with pytest.raises(ValueError, match=r"not a directory: 'README.md'.*blob"):
        await gh_list_repository_tree(
            "octo",
            "repo",
            commit_sha,
            path="README.md",
            ctx=_context(blob_client),
        )


async def test_mismatched_commit_and_tree_evidence_fail_closed() -> None:
    commit_sha = "a" * 40
    mismatch_client = FakeGhClient([_commit("b" * 40, "c" * 40)])

    with pytest.raises(RuntimeError, match="did not preserve the requested commit SHA"):
        await gh_list_repository_tree(
            "octo",
            "repo",
            commit_sha,
            ctx=_context(mismatch_client),
        )

    root_sha = "c" * 40
    tree_client = FakeGhClient(
        [
            _commit(commit_sha, root_sha),
            _tree("d" * 40, []),
        ]
    )
    with pytest.raises(RuntimeError, match="did not preserve the requested tree SHA"):
        await gh_list_repository_tree(
            "octo",
            "repo",
            commit_sha,
            ctx=_context(tree_client),
        )


@pytest.mark.parametrize(
    ("type", "mode"),
    [("blob", "040000"), ("tree", "100644"), ("commit", "100644"), ("tag", "100644")],
)
async def test_malformed_tree_object_type_or_mode_fails_closed(type: str, mode: str) -> None:
    commit_sha = "a" * 40
    root_sha = "b" * 40
    client = FakeGhClient(
        [
            _commit(commit_sha, root_sha),
            _tree(root_sha, [_entry("bad", type=type, mode=mode, sha="c" * 40)]),
        ]
    )

    with pytest.raises(RuntimeError, match="malformed Git tree entry"):
        await gh_list_repository_tree(
            "octo",
            "repo",
            commit_sha,
            ctx=_context(client),
        )


async def test_application_entry_bound_is_explicitly_incomplete() -> None:
    commit_sha = "a" * 40
    root_sha = "b" * 40
    client = FakeGhClient(
        [
            _commit(commit_sha, root_sha),
            _tree(
                root_sha,
                [
                    _entry("a.txt", type="blob", mode="100644", sha="c" * 40, size=1),
                    _entry("b.txt", type="blob", mode="100644", sha="d" * 40, size=1),
                ],
            ),
        ]
    )

    result = await gh_list_repository_tree(
        "octo",
        "repo",
        commit_sha,
        max_entries=1,
        ctx=_context(client),
    )

    assert [item.path for item in result.entries] == ["a.txt"]
    assert result.entries_returned == 1
    assert result.truncated is True
    assert result.evidence_complete is False
    assert result.warning is not None
    assert "max_entries" in result.warning


async def test_github_source_truncation_is_explicitly_incomplete() -> None:
    commit_sha = "a" * 40
    root_sha = "b" * 40
    client = FakeGhClient(
        [
            _commit(commit_sha, root_sha),
            _tree(
                root_sha,
                [_entry("a.txt", type="blob", mode="100644", sha="c" * 40, size=1)],
                truncated=True,
            ),
        ]
    )

    result = await gh_list_repository_tree(
        "octo",
        "repo",
        commit_sha,
        recursive=True,
        ctx=_context(client),
    )

    assert result.truncated is True
    assert result.evidence_complete is False
    assert result.warning is not None
    assert "GitHub reported truncated" in result.warning


async def test_truncated_traversal_evidence_fails_closed() -> None:
    commit_sha = "a" * 40
    root_sha = "b" * 40
    client = FakeGhClient(
        [
            _commit(commit_sha, root_sha),
            _tree(
                root_sha,
                [_entry("docs", type="tree", mode="040000", sha="c" * 40)],
                truncated=True,
            ),
        ]
    )

    with pytest.raises(RuntimeError, match="truncated directory traversal evidence"):
        await gh_list_repository_tree(
            "octo",
            "repo",
            commit_sha,
            path="docs",
            ctx=_context(client),
        )


async def test_public_schema_annotations_and_file_read_contract() -> None:
    tools: dict[str, Any] = {tool.name: tool for tool in await mcp.list_tools()}
    tree_tool = tools["gh_list_repository_tree"]

    assert tree_tool.annotations.read_only_hint is True
    assert tree_tool.annotations.destructive_hint is False
    assert tree_tool.annotations.idempotent_hint is True
    assert tree_tool.annotations.open_world_hint is True
    properties = tree_tool.input_schema["properties"]
    assert properties["commit_sha"]["pattern"] == r"^[0-9A-Fa-f]{40}$"
    assert properties["path"]["default"] == ""
    assert properties["path"]["maxLength"] == 4096
    assert properties["recursive"]["default"] is False
    assert properties["max_entries"]["anyOf"][0]["minimum"] == 1
    output = tree_tool.output_schema["properties"]
    assert output["commit_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert output["root_tree_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert output["directory_tree_sha"]["pattern"] == r"^[0-9a-f]{40}$"
    assert output["truncated"]["type"] == "boolean"
    assert output["evidence_complete"]["type"] == "boolean"

    file_schema = tools["gh_get_file_contents"].input_schema
    assert set(file_schema["properties"]) == {"owner", "repo", "path", "ref"}
    assert set(file_schema["required"]) == {"owner", "repo", "path", "ref"}
    assert file_schema["properties"]["path"]["minLength"] == 1
    assert file_schema["properties"]["ref"]["maxLength"] == 1024
