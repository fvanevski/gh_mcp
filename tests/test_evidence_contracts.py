"""Focused regressions for the shared bounded-evidence contract."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from mcp_gh_server.evidence import (
    BoundedTextAccumulator,
    BoundedTextEvidence,
    bound_text_evidence,
    pagination_evidence,
)


def test_exact_full_page_is_not_off_by_one_has_more() -> None:
    evidence = pagination_evidence(
        page=1,
        requested_per_page=100,
        default_per_page=30,
        hard_max_results=100,
        returned_count=100,
        total_count=100,
    )
    assert evidence.has_more is False
    assert evidence.truncated is False
    assert evidence.warning is None


def test_one_item_beyond_full_page_has_more() -> None:
    evidence = pagination_evidence(
        page=1,
        requested_per_page=100,
        default_per_page=30,
        hard_max_results=100,
        returned_count=100,
        total_count=101,
    )
    assert evidence.has_more is True
    assert evidence.truncated is False


def test_last_page_uses_authoritative_total() -> None:
    evidence = pagination_evidence(
        page=3,
        requested_per_page=100,
        default_per_page=30,
        hard_max_results=100,
        returned_count=1,
        total_count=201,
    )
    assert evidence.has_more is False
    assert evidence.truncated is False


def test_hard_cap_is_explicit_when_more_results_exist() -> None:
    evidence = pagination_evidence(
        page=1,
        requested_per_page=500,
        default_per_page=30,
        hard_max_results=100,
        returned_count=100,
        total_count=150,
    )
    assert evidence.per_page == 100
    assert evidence.has_more is True
    assert evidence.truncated is True
    assert evidence.warning is not None
    assert "hard limit" in evidence.warning


def test_empty_page_is_complete() -> None:
    evidence = pagination_evidence(
        page=1,
        requested_per_page=None,
        default_per_page=30,
        hard_max_results=100,
        returned_count=0,
        total_count=0,
    )
    assert evidence.total_count == 0
    assert evidence.has_more is False
    assert evidence.truncated is False


def test_unknown_total_requires_explicit_has_more() -> None:
    with pytest.raises(ValueError, match="has_more is required"):
        pagination_evidence(
            page=1,
            requested_per_page=30,
            default_per_page=30,
            hard_max_results=100,
            returned_count=30,
        )


def test_short_authoritative_page_is_marked_incomplete() -> None:
    evidence = pagination_evidence(
        page=1,
        requested_per_page=30,
        default_per_page=30,
        hard_max_results=100,
        returned_count=29,
        total_count=30,
    )
    assert evidence.truncated is True
    assert evidence.warning is not None
    assert "incomplete" in evidence.warning


def test_complete_text_reports_bytes_and_full_digest() -> None:
    content = "alpha 😀 omega"
    evidence = bound_text_evidence(
        content,
        requested_max_bytes=100,
        hard_max_bytes=100,
    )
    encoded = content.encode()
    assert evidence.content == content
    assert evidence.bytes_returned == len(encoded)
    assert evidence.total_bytes == len(encoded)
    assert evidence.truncated is False
    assert evidence.sha256 == hashlib.sha256(encoded).hexdigest()
    assert evidence.warning is None


def test_utf8_truncation_preserves_true_prefix() -> None:
    content = "ab😀cd"
    evidence = bound_text_evidence(
        content,
        requested_max_bytes=5,
        hard_max_bytes=5,
        label="Log evidence",
    )
    assert evidence.content == "ab"
    assert evidence.bytes_returned == 2
    assert evidence.total_bytes == 8
    assert evidence.truncated is True
    assert evidence.sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert evidence.warning is not None
    assert "returned 2 of 8" in evidence.warning


def test_hard_byte_cap_overrides_requested_limit() -> None:
    evidence = bound_text_evidence(
        "abcdef",
        requested_max_bytes=10,
        hard_max_bytes=4,
    )
    assert evidence.content == "abcd"
    assert evidence.bytes_returned == 4
    assert evidence.total_bytes == 6
    assert evidence.truncated is True
    assert evidence.warning is not None
    assert "hard limit" in evidence.warning


def test_empty_text_has_stable_digest() -> None:
    evidence = bound_text_evidence(
        "",
        requested_max_bytes=None,
        hard_max_bytes=10,
    )
    assert evidence.content == ""
    assert evidence.bytes_returned == 0
    assert evidence.total_bytes == 0
    assert evidence.truncated is False
    assert evidence.sha256 == hashlib.sha256(b"").hexdigest()


def test_accumulator_stays_bounded_but_hashes_all_chunks() -> None:
    accumulator = BoundedTextAccumulator(
        requested_max_bytes=5,
        hard_max_bytes=5,
    )
    accumulator.add_text("ab😀")
    accumulator.add_text("cd")
    evidence = accumulator.finish()
    assert evidence.content == "ab"
    assert evidence.bytes_returned == 2
    assert evidence.total_bytes == 8
    assert evidence.sha256 == hashlib.sha256("ab😀cd".encode()).hexdigest()


def test_truncated_model_cannot_omit_warning() -> None:
    with pytest.raises(ValidationError, match="requires an explicit warning"):
        BoundedTextEvidence(
            content="a",
            bytes_returned=1,
            total_bytes=2,
            truncated=True,
            sha256=hashlib.sha256(b"ab").hexdigest(),
        )


def test_evidence_models_remain_json_safe_through_serialization_layer() -> None:
    from mcp_gh_server.serialization import to_json_value

    evidence = bound_text_evidence(
        "abc",
        requested_max_bytes=3,
        hard_max_bytes=3,
    )
    dumped = evidence.model_dump(mode="python")
    assert to_json_value(dumped) == evidence.model_dump(mode="json")


def test_legacy_bounded_utf8_projection_preserves_tuple_contract() -> None:
    from mcp_gh_server.tooling import bounded_utf8

    evidence = bound_text_evidence(
        "ab😀cd",
        requested_max_bytes=5,
        hard_max_bytes=5,
    )
    assert bounded_utf8("ab😀cd", 5) == (
        evidence.content,
        evidence.bytes_returned,
        evidence.total_bytes,
        evidence.truncated,
        evidence.sha256,
    )
