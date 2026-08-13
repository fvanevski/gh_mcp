"""Streaming bounded selection semantics for GitHub Actions log evidence."""

from __future__ import annotations

import hashlib

from .evidence import BoundedTextEvidence

# C0 controls (U+0000-U+001F) except TAB(0x09), LF(0x0A), CR(0x0D); DEL(U+007F);
# C1 controls (U+0080-U+009F). All are replaced by their literal hex escape form so
# that sha256 / byte counting / marker matching operate on inert plaintext.
_NORMALIZATION_TABLE: dict[int, str] = {
    i: f"\\x{i:02x}" for i in range(128) if (i < 0x20 and i not in {0x09, 0x0A, 0x0D}) or i == 0x7F
}
for _i in range(0x80, 0xA0):
    _NORMALIZATION_TABLE[_i] = f"\\x{_i:02x}"


def _normalize_terminal_controls(text: str) -> str:
    """Replace terminal control characters with inert visible hex-escape text."""

    return text.translate(_NORMALIZATION_TABLE)


class _Utf8PrefixCollector:
    """Retain at most one valid UTF-8 prefix without buffering the full source."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._parts: list[str] = []
        self._bytes_returned = 0
        self._closed = False

    @property
    def bytes_returned(self) -> int:
        return self._bytes_returned

    @property
    def content(self) -> str:
        return "".join(self._parts)

    def add_text(self, chunk: str) -> None:
        if self._closed or not chunk:
            return
        encoded = chunk.encode("utf-8")
        remaining = self._limit - self._bytes_returned
        if remaining <= 0:
            self._closed = True
            return
        if len(encoded) <= remaining:
            self._parts.append(chunk)
            self._bytes_returned += len(encoded)
            return

        prefix = encoded[:remaining].decode("utf-8", errors="ignore")
        if prefix:
            self._parts.append(prefix)
            self._bytes_returned += len(prefix.encode("utf-8"))
        self._closed = True


class ActionLogEvidenceAccumulator:
    """Hash/count a complete log stream while retaining only bounded selected text.

    Input chunks are expected to be valid Python text. The accumulator hashes their UTF-8
    encoding, keeps only the configured returned evidence, and supports literal markers
    across chunk boundaries without retaining the full source in memory.
    """

    def __init__(
        self,
        *,
        requested_max_bytes: int | None,
        hard_max_bytes: int,
        tail_bytes: int | None = None,
        start_marker: str | None = None,
        end_marker: str | None = None,
        label: str = "Actions log evidence",
    ) -> None:
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

        self._requested = requested
        self._hard_max_bytes = hard_max_bytes
        self._effective_limit = min(requested, hard_max_bytes)
        self._tail_bytes = tail_bytes
        self._start_marker = start_marker
        self._end_marker = end_marker
        self._label = label

        self._hasher = hashlib.sha256()
        self._total_bytes = 0
        self._collector = _Utf8PrefixCollector(self._effective_limit)
        self._tail = bytearray()

        self._selection_active = start_marker is None
        self._selection_ended = False
        self._start_found = start_marker is None
        self._end_found = end_marker is None
        self._start_tail = ""
        self._end_tail = ""

    def add_text(self, chunk: str) -> None:
        """Consume one decoded text chunk without retaining unselected source text."""

        if not isinstance(chunk, str):
            raise TypeError("action log chunks must be text")
        if not chunk:
            return

        chunk = _normalize_terminal_controls(chunk)
        encoded = chunk.encode("utf-8")
        self._hasher.update(encoded)
        self._total_bytes += len(encoded)

        if self._tail_bytes is not None:
            tail_limit = min(self._tail_bytes, self._effective_limit)
            if tail_limit <= 0:
                return
            if len(encoded) >= tail_limit:
                self._tail = bytearray(encoded[-tail_limit:])
            else:
                self._tail.extend(encoded)
                if len(self._tail) > tail_limit:
                    del self._tail[: len(self._tail) - tail_limit]
            return

        if self._selection_ended:
            return

        if not self._selection_active:
            assert self._start_marker is not None
            candidate = self._start_tail + chunk
            marker_index = candidate.find(self._start_marker)
            if marker_index < 0:
                keep = max(len(self._start_marker) - 1, 0)
                self._start_tail = candidate[-keep:] if keep else ""
                return
            self._selection_active = True
            self._start_found = True
            self._start_tail = ""
            self._consume_selected(candidate[marker_index:])
            return

        self._consume_selected(chunk)

    def _consume_selected(self, chunk: str) -> None:
        if self._selection_ended or not chunk:
            return
        if self._end_marker is None:
            self._collector.add_text(chunk)
            return

        candidate = self._end_tail + chunk
        marker_index = candidate.find(self._end_marker)
        if marker_index >= 0:
            end_index = marker_index + len(self._end_marker)
            self._collector.add_text(candidate[:end_index])
            self._end_tail = ""
            self._end_found = True
            self._selection_ended = True
            return

        keep = max(len(self._end_marker) - 1, 0)
        if keep == 0:
            self._collector.add_text(candidate)
            self._end_tail = ""
            return
        if len(candidate) <= keep:
            self._end_tail = candidate
            return

        self._collector.add_text(candidate[:-keep])
        self._end_tail = candidate[-keep:]

    def finish(self) -> BoundedTextEvidence:
        """Finalize the bounded evidence and validate requested literal markers."""

        if self._start_marker is not None and not self._start_found:
            raise ValueError("start_marker was not found in the retrieved log")
        if self._end_marker is not None and not self._end_found:
            raise ValueError("end_marker was not found at or after the selected start")

        selection_notes: list[str] = []
        if self._tail_bytes is not None:
            returned = bytes(self._tail).decode("utf-8", errors="ignore")
            bytes_returned = len(returned.encode("utf-8"))
            if self._tail_bytes > self._effective_limit:
                selection_notes.append(
                    f"Requested tail_bytes={self._tail_bytes} was limited to the effective "
                    f"max_bytes={self._effective_limit}."
                )
            if bytes_returned < self._total_bytes:
                selection_notes.append(
                    f"Selected a UTF-8-safe suffix of {bytes_returned} bytes "
                    f"(up to {min(self._tail_bytes, self._effective_limit)} requested)."
                )
        else:
            returned = self._collector.content
            bytes_returned = self._collector.bytes_returned
            if self._start_marker is not None:
                selection_notes.append("Applied inclusive literal start_marker selection.")
            if self._end_marker is not None:
                selection_notes.append("Applied inclusive literal end_marker selection.")

        truncated = bytes_returned < self._total_bytes
        warnings: list[str] = []
        if self._requested > self._hard_max_bytes:
            warnings.append(
                f"Requested max_bytes={self._requested} was capped at the server hard limit of "
                f"{self._hard_max_bytes} bytes."
            )
        warnings.extend(selection_notes)
        if truncated:
            warnings.append(
                f"{self._label} is incomplete: returned {bytes_returned} of "
                f"{self._total_bytes} UTF-8 bytes. sha256 fingerprints the complete "
                "normalized evidence stream before selection."
            )

        return BoundedTextEvidence(
            content=returned,
            bytes_returned=bytes_returned,
            total_bytes=self._total_bytes,
            truncated=truncated,
            sha256=self._hasher.hexdigest(),
            warning=" ".join(warnings) or None,
        )


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
    """Compatibility wrapper for one already-materialized source string."""

    accumulator = ActionLogEvidenceAccumulator(
        requested_max_bytes=requested_max_bytes,
        hard_max_bytes=hard_max_bytes,
        tail_bytes=tail_bytes,
        start_marker=start_marker,
        end_marker=end_marker,
        label=label,
    )
    accumulator.add_text(content)
    return accumulator.finish()
