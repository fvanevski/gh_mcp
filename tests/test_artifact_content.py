"""Regression coverage for bounded safe GitHub Actions artifact-content inspection."""

from __future__ import annotations

import hashlib
import io
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_gh_server.server import AppContext
from mcp_gh_server.settings import Settings
from mcp_gh_server.tools import artifact_content
from mcp_gh_server.tools.artifact_content import (
    gh_list_artifact_files,
    gh_read_artifact_file,
)


@dataclass
class FakeGhClient:
    """Return queued metadata payloads; archive bytes are injected separately."""

    results: list[Any]
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _context(client: FakeGhClient, settings: Settings | None = None) -> Any:
    app = AppContext(
        client=client,  # type: ignore[arg-type]
        settings=settings or Settings(),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _artifact(
    *,
    artifact_id: int = 77,
    expired: bool = False,
    size_in_bytes: int = 4096,
) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "name": "evidence",
        "size_in_bytes": size_in_bytes,
        "digest": "sha256:" + "d" * 64,
        "expired": expired,
        "created_at": "2026-08-12T10:00:00Z",
        "expires_at": "2026-11-10T10:00:00Z",
        "workflow_run": {
            "id": 123,
            "head_sha": "a" * 40,
        },
    }


def _zip_bytes(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _install_archive(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    *,
    calls: list[tuple[str, ...]] | None = None,
) -> None:
    async def fake_stream(
        client: object,
        *args: str,
        on_chunk: Any,
        timeout: float | None = None,
    ) -> object:
        del client, timeout
        if calls is not None:
            calls.append(args)
        on_chunk(payload)
        return SimpleNamespace(attempts=1)

    monkeypatch.setattr(artifact_content, "stream_governed_bytes", fake_stream)


async def test_list_artifact_files_returns_normalized_bounded_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip_bytes(
        [
            ("zeta.txt", b"z"),
            ("nested/alpha.json", b'{"ok": true}\n'),
        ]
    )
    stream_calls: list[tuple[str, ...]] = []
    _install_archive(monkeypatch, archive, calls=stream_calls)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    result = await gh_list_artifact_files(
        "octo",
        "repo",
        77,
        ctx=_context(client),
        page=1,
        per_page=1,
    )

    assert client.calls[0][0] == (
        "api",
        "repos/octo/repo/actions/artifacts/77",
        "-X",
        "GET",
    )
    assert stream_calls == [
        (
            "api",
            "repos/octo/repo/actions/artifacts/77/zip",
            "-X",
            "GET",
        )
    ]
    assert result.artifact_id == 77
    assert result.artifact_digest == "sha256:" + "d" * 64
    assert result.workflow_run_id == 123
    assert result.workflow_head_sha == "a" * 40
    assert result.archive_bytes == len(archive)
    assert result.archive_sha256 == hashlib.sha256(archive).hexdigest()
    assert result.total_count == 2
    assert result.page == 1
    assert result.per_page == 1
    assert result.has_more is True
    assert result.truncated is False
    assert [entry.path for entry in result.files] == ["nested/alpha.json"]


async def test_list_artifact_files_reports_server_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip_bytes([("a.txt", b"a"), ("b.txt", b"b")])
    _install_archive(monkeypatch, archive)
    settings = Settings(default_max_results=1, hard_max_results=1)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    result = await gh_list_artifact_files(
        "octo",
        "repo",
        77,
        ctx=_context(client, settings),
        per_page=2,
    )

    assert result.per_page == 1
    assert result.has_more is True
    assert result.truncated is True
    assert result.warning is not None
    assert "capped at the server hard limit of 1" in result.warning


async def test_read_artifact_file_returns_bounded_complete_file_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_text = "alpha β gamma\n"
    archive = _zip_bytes([("reports/result.json", full_text.encode())])
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    result = await gh_read_artifact_file(
        "octo",
        "repo",
        77,
        "reports/result.json",
        ctx=_context(client),
        max_bytes=7,
    )

    assert result.path == "reports/result.json"
    assert result.encoding == "utf-8"
    assert result.content == "alpha "
    assert result.bytes_returned == 6
    assert result.total_bytes == len(full_text.encode())
    assert result.truncated is True
    assert result.warning is not None
    assert result.sha256 == hashlib.sha256(full_text.encode()).hexdigest()


async def test_expired_artifact_fails_before_archive_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = False

    async def unexpected_stream(*args: Any, **kwargs: Any) -> object:
        nonlocal attempted
        attempted = True
        raise AssertionError("archive retrieval must not run")

    monkeypatch.setattr(artifact_content, "stream_governed_bytes", unexpected_stream)
    client = FakeGhClient([_artifact(expired=True)])

    with pytest.raises(RuntimeError, match="expired"):
        await gh_list_artifact_files("octo", "repo", 77, ctx=_context(client))

    assert attempted is False


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("../escape.txt", "path traversal"),
        ("/absolute.txt", "absolute path"),
        (r"C:\absolute.txt", "absolute path"),
    ],
)
async def test_archive_rejects_traversal_and_absolute_paths(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    message: str,
) -> None:
    archive = _zip_bytes([(name, b"unsafe")])
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    with pytest.raises(RuntimeError, match=message):
        await gh_list_artifact_files("octo", "repo", 77, ctx=_context(client))


@pytest.mark.parametrize(
    "entries",
    [
        [("a//b.txt", b"one"), ("a/b.txt", b"two")],
        [("folder", b"file"), ("folder/child.txt", b"child")],
        [("folder/child.txt", b"child"), ("folder", b"file")],
    ],
)
async def test_archive_rejects_duplicate_or_conflicting_normalized_entries(
    monkeypatch: pytest.MonkeyPatch,
    entries: list[tuple[str, bytes]],
) -> None:
    archive = _zip_bytes(entries)
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    with pytest.raises(RuntimeError, match=r"duplicate|conflict"):
        await gh_list_artifact_files("octo", "repo", 77, ctx=_context(client))


async def test_archive_rejects_escaping_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = _zip_bytes([(link, b"../../outside")])
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    with pytest.raises(RuntimeError, match="symbolic link"):
        await gh_list_artifact_files("octo", "repo", 77, ctx=_context(client))


async def test_reported_oversized_archive_fails_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = False

    async def unexpected_stream(*args: Any, **kwargs: Any) -> object:
        nonlocal attempted
        attempted = True
        raise AssertionError("archive retrieval must not run")

    monkeypatch.setattr(artifact_content, "_MAX_ARTIFACT_ARCHIVE_BYTES", 10)
    monkeypatch.setattr(artifact_content, "stream_governed_bytes", unexpected_stream)
    client = FakeGhClient([_artifact(size_in_bytes=11)])

    with pytest.raises(RuntimeError, match="archive hard limit"):
        await gh_list_artifact_files("octo", "repo", 77, ctx=_context(client))

    assert attempted is False


async def test_streamed_oversized_archive_is_stopped_and_temp_state_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_content, "_MAX_ARTIFACT_ARCHIVE_BYTES", 10)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    _install_archive(monkeypatch, b"x" * 11)
    client = FakeGhClient([_artifact(size_in_bytes=5)])

    with pytest.raises(RuntimeError, match="archive exceeded"):
        await gh_list_artifact_files("octo", "repo", 77, ctx=_context(client))

    assert list(tmp_path.iterdir()) == []


async def test_archive_rejects_oversized_total_uncompressed_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_content, "_MAX_ARTIFACT_UNCOMPRESSED_BYTES", 3)
    archive = _zip_bytes([("evidence.txt", b"four")])
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    with pytest.raises(RuntimeError, match="uncompressed size"):
        await gh_list_artifact_files("octo", "repo", 77, ctx=_context(client))


async def test_read_rejects_oversized_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_content, "_MAX_ARTIFACT_FILE_BYTES", 3)
    archive = _zip_bytes([("evidence.txt", b"four")])
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    with pytest.raises(RuntimeError, match="file hard limit"):
        await gh_read_artifact_file(
            "octo",
            "repo",
            77,
            "evidence.txt",
            ctx=_context(client),
        )


async def test_read_rejects_non_utf8_binary_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip_bytes([("binary.dat", b"\xff\xfe\x00")])
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    with pytest.raises(RuntimeError, match="not valid UTF-8"):
        await gh_read_artifact_file(
            "octo",
            "repo",
            77,
            "binary.dat",
            ctx=_context(client),
        )


@pytest.mark.parametrize("path", ["a//b.txt", "./a.txt", "../a.txt", "/a.txt", r"C:\a.txt"])
async def test_read_requires_exact_normalized_requested_path(path: str) -> None:
    client = FakeGhClient([])

    with pytest.raises(ValueError, match=r"normalized|traversal|absolute"):
        await gh_read_artifact_file(
            "octo",
            "repo",
            77,
            path,
            ctx=_context(client),
        )

    assert client.calls == []


async def test_temp_archive_state_is_removed_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip_bytes([("evidence.txt", b"ok")])
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    result = await gh_read_artifact_file(
        "octo",
        "repo",
        77,
        "evidence.txt",
        ctx=_context(client),
    )

    assert result.content == "ok"
    assert list(tmp_path.iterdir()) == []


async def test_archive_at_exact_download_hard_limit_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip_bytes([("evidence.txt", b"ok")])
    monkeypatch.setattr(artifact_content, "_MAX_ARTIFACT_ARCHIVE_BYTES", len(archive))
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    result = await gh_list_artifact_files("octo", "repo", 77, ctx=_context(client))

    assert result.archive_bytes == len(archive)
    assert [entry.path for entry in result.files] == ["evidence.txt"]


async def test_archive_at_exact_uncompressed_hard_limit_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_content, "_MAX_ARTIFACT_UNCOMPRESSED_BYTES", 4)
    archive = _zip_bytes([("evidence.txt", b"four")])
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    result = await gh_list_artifact_files("octo", "repo", 77, ctx=_context(client))

    assert result.files[0].size_in_bytes == 4


async def test_file_at_exact_read_hard_limit_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_content, "_MAX_ARTIFACT_FILE_BYTES", 4)
    archive = _zip_bytes([("evidence.txt", b"four")])
    _install_archive(monkeypatch, archive)
    client = FakeGhClient([_artifact(size_in_bytes=len(archive))])

    result = await gh_read_artifact_file(
        "octo",
        "repo",
        77,
        "evidence.txt",
        ctx=_context(client),
    )

    assert result.content == "four"
    assert result.bytes_returned == 4
    assert result.total_bytes == 4
    assert result.truncated is False
