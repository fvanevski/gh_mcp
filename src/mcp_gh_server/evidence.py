"""Shared bounded-evidence and pagination completeness contracts."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field, model_validator


class PaginationEvidence(BaseModel):
    """Completeness metadata for one explicitly bounded result page."""

    total_count: int | None = Field(default=None, ge=0)
    page: int = Field(ge=1)
    per_page: int = Field(ge=1)
    returned_count: int = Field(ge=0)
    has_more: bool
    truncated: bool
    warning: str | None = None

    @model_validator(mode="after")
    def validate_completeness(self) -> PaginationEvidence:
        if self.returned_count > self.per_page:
            raise ValueError("returned_count cannot exceed per_page")
        if self.truncated and not self.warning:
            raise ValueError("truncated pagination evidence requires an explicit warning")
        return self


class BoundedTextEvidence(BaseModel):
    """Bounded UTF-8 text plus byte accounting and a complete-evidence digest."""

    content: str
    bytes_returned: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    truncated: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warning: str | None = None

    @model_validator(mode="after")
    def validate_completeness(self) -> BoundedTextEvidence:
        actual_returned = len(self.content.encode("utf-8"))
        if self.bytes_returned != actual_returned:
            raise ValueError("bytes_returned must equal the UTF-8 byte length of content")
        if self.bytes_returned > self.total_bytes:
            raise ValueError("bytes_returned cannot exceed total_bytes")
        if self.truncated != (self.bytes_returned < self.total_bytes):
            raise ValueError("truncated must reflect whether returned bytes are incomplete")
        if self.truncated and not self.warning:
            raise ValueError("truncated text evidence requires an explicit warning")
        return self


class BoundedTextAccumulator:
    """Hash complete text while retaining at most the configured UTF-8 byte prefix."""

    def __init__(
        self,
        *,
        requested_max_bytes: int | None,
        hard_max_bytes: int,
        label: str = "Evidence",
    ) -> None:
        if hard_max_bytes < 1:
            raise ValueError("hard_max_bytes must be positive")
        requested = hard_max_bytes if requested_max_bytes is None else requested_max_bytes
        if requested < 1:
            raise ValueError("requested_max_bytes must be positive")

        self._requested_max_bytes = requested
        self._hard_max_bytes = hard_max_bytes
        self._limit = min(requested, hard_max_bytes)
        self._label = label
        self._content_parts: list[str] = []
        self._bytes_returned = 0
        self._total_bytes = 0
        self._hasher = hashlib.sha256()
        self._prefix_closed = False

    @property
    def limit(self) -> int:
        """Effective retained-byte limit after applying the hard cap."""

        return self._limit

    def add_text(self, chunk: str) -> None:
        """Account for one text chunk while retaining only a valid UTF-8 prefix."""

        encoded = chunk.encode("utf-8")
        self._hasher.update(encoded)
        self._total_bytes += len(encoded)
        if self._prefix_closed or not encoded:
            return

        remaining = self._limit - self._bytes_returned
        if len(encoded) <= remaining:
            self._content_parts.append(chunk)
            self._bytes_returned += len(encoded)
            return

        prefix = encoded[:remaining].decode("utf-8", errors="ignore")
        if prefix:
            self._content_parts.append(prefix)
            self._bytes_returned += len(prefix.encode("utf-8"))
        self._prefix_closed = True

    def finish(self) -> BoundedTextEvidence:
        """Return typed completeness metadata for all text supplied so far."""

        truncated = self._bytes_returned < self._total_bytes
        warnings: list[str] = []
        if self._requested_max_bytes > self._hard_max_bytes:
            warnings.append(
                f"Requested max_bytes={self._requested_max_bytes} was capped at the server "
                f"hard limit of {self._hard_max_bytes} bytes."
            )
        if truncated:
            warnings.append(
                f"{self._label} was truncated: returned {self._bytes_returned} of "
                f"{self._total_bytes} UTF-8 bytes. sha256 fingerprints the complete evidence."
            )
        return BoundedTextEvidence(
            content="".join(self._content_parts),
            bytes_returned=self._bytes_returned,
            total_bytes=self._total_bytes,
            truncated=truncated,
            sha256=self._hasher.hexdigest(),
            warning=" ".join(warnings) or None,
        )


def bound_text_evidence(
    content: str,
    *,
    requested_max_bytes: int | None,
    hard_max_bytes: int,
    label: str = "Evidence",
) -> BoundedTextEvidence:
    """Return bounded UTF-8 text using the shared byte/digest contract."""

    accumulator = BoundedTextAccumulator(
        requested_max_bytes=requested_max_bytes,
        hard_max_bytes=hard_max_bytes,
        label=label,
    )
    accumulator.add_text(content)
    return accumulator.finish()


def pagination_evidence(
    *,
    page: int,
    requested_per_page: int | None,
    default_per_page: int,
    hard_max_results: int,
    returned_count: int,
    total_count: int | None = None,
    has_more: bool | None = None,
) -> PaginationEvidence:
    """Resolve page completeness without inferring it from a merely full page."""

    if page < 1:
        raise ValueError("page must be positive")
    if default_per_page < 1:
        raise ValueError("default_per_page must be positive")
    if hard_max_results < 1:
        raise ValueError("hard_max_results must be positive")
    requested = default_per_page if requested_per_page is None else requested_per_page
    if requested < 1:
        raise ValueError("requested_per_page must be positive")
    if returned_count < 0:
        raise ValueError("returned_count cannot be negative")

    per_page = min(requested, hard_max_results)
    if returned_count > per_page:
        raise ValueError("returned_count cannot exceed the effective per_page bound")

    incomplete_page = False
    if total_count is not None:
        if total_count < 0:
            raise ValueError("total_count cannot be negative")
        offset = (page - 1) * per_page
        expected_count = min(per_page, max(total_count - offset, 0))
        if returned_count > expected_count:
            raise ValueError("returned_count exceeds the authoritative total for this page")
        computed_has_more = offset + expected_count < total_count
        if has_more is not None and has_more != computed_has_more:
            raise ValueError("has_more conflicts with authoritative total_count")
        resolved_has_more = computed_has_more
        incomplete_page = returned_count < expected_count
    else:
        if has_more is None:
            raise ValueError(
                "has_more is required when total_count is unknown; do not infer completeness "
                "from a full page"
            )
        resolved_has_more = has_more

    hard_cap_hit = requested > hard_max_results
    hard_cap_truncated = hard_cap_hit and resolved_has_more
    truncated = incomplete_page or hard_cap_truncated
    warnings: list[str] = []
    if hard_cap_hit:
        warnings.append(
            f"Requested per_page={requested} was capped at the server hard limit of "
            f"{hard_max_results}."
        )
    if incomplete_page:
        warnings.append(
            "Returned page is shorter than the authoritative count for this page; evidence "
            "is incomplete."
        )
    if hard_cap_truncated:
        warnings.append("Additional results exist beyond the server hard result cap.")

    return PaginationEvidence(
        total_count=total_count,
        page=page,
        per_page=per_page,
        returned_count=returned_count,
        has_more=resolved_has_more,
        truncated=truncated,
        warning=" ".join(warnings) or None,
    )
