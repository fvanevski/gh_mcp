"""Bounded selection semantics for GitHub Actions log evidence."""

from __future__ import annotations

import hashlib

from .evidence import BoundedTextEvidence


def _utf8_prefix(content: str, max_bytes: int) -> str:
    """Return at most ``max_bytes`` as a valid UTF-8 prefix."""

    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _utf8_suffix(content: str, max_bytes: int) -> str:
    """Return at most ``max_bytes`` as a valid UTF-8 suffix."""

    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def select_action_log_evidence(
    content: str,
    *,
    requested_max_bytes: int | None,
    hard_max_bytes: int,
    tail_bytes: int | None = None,
    start_marker: str | None = None,
    end_marker: str | None = None,
    label: str = "Actions log evidence",
) -> BoundedTextEvidence:
    """Select literal log evidence while hashing the complete retrieved source text.

    Marker selection is inclusive. ``end_marker`` is resolved at or after the selected
    start. Tail selection is mutually exclusive with markers. ``sha256`` always hashes
    the complete ``content`` argument, before marker/tail/max-byte selection.
    """

    if hard_max_bytes < 1:
        raise ValueError("hard_max_bytes must be positive")
    requested = hard_max_bytes if requested_max_bytes is None else requested_max_bytes
    if requested < 1:
        raise ValueError("max_bytes must be positive")
    if tail_bytes is not None and tail_bytes < 1:
        raise ValueError("tail_bytes must be positive")
    if tail_bytes is not None and (start_marker is not None or end_marker is not None):
        raise ValueError("tail_bytes cannot be combined with start_marker or end_marker")
    if start_marker == "" or end_marker == "":
        raise ValueError("markers must not be empty")

    full_bytes = content.encode("utf-8")
    total_bytes = len(full_bytes)
    digest = hashlib.sha256(full_bytes).hexdigest()
    effective_limit = min(requested, hard_max_bytes)
    selection_notes: list[str] = []

    if tail_bytes is not None:
        tail_limit = min(tail_bytes, effective_limit)
        returned = _utf8_suffix(content, tail_limit)
        if tail_bytes > effective_limit:
            selection_notes.append(
                f"Requested tail_bytes={tail_bytes} was limited to the effective "
                f"max_bytes={effective_limit}."
            )
        if len(returned.encode("utf-8")) < total_bytes:
            selection_notes.append(f"Selected the final {tail_limit} UTF-8 bytes.")
    else:
        start_index = 0
        if start_marker is not None:
            start_index = content.find(start_marker)
            if start_index < 0:
                raise ValueError("start_marker was not found in the retrieved log")
            selection_notes.append("Applied inclusive literal start_marker selection.")

        end_index = len(content)
        if end_marker is not None:
            marker_index = content.find(end_marker, start_index)
            if marker_index < 0:
                raise ValueError("end_marker was not found at or after the selected start")
            end_index = marker_index + len(end_marker)
            selection_notes.append("Applied inclusive literal end_marker selection.")

        selected = content[start_index:end_index]
        returned = _utf8_prefix(selected, effective_limit)

    bytes_returned = len(returned.encode("utf-8"))
    truncated = bytes_returned < total_bytes

    warnings: list[str] = []
    if requested > hard_max_bytes:
        warnings.append(
            f"Requested max_bytes={requested} was capped at the server hard limit of "
            f"{hard_max_bytes} bytes."
        )
    warnings.extend(selection_notes)
    if truncated:
        warnings.append(
            f"{label} is incomplete: returned {bytes_returned} of {total_bytes} UTF-8 bytes. "
            "sha256 fingerprints the complete retrieved source log before selection."
        )

    return BoundedTextEvidence(
        content=returned,
        bytes_returned=bytes_returned,
        total_bytes=total_bytes,
        truncated=truncated,
        sha256=digest,
        warning=" ".join(warnings) or None,
    )
