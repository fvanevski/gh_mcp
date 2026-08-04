"""GitHub CLI subprocess runner with JSON output parsing and serialization."""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .serialization import to_json_value

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GhClient:
    """Runs ``gh`` commands via subprocess and parses JSON output."""

    settings: Any  # Settings (typed via import to avoid circular dep)

    def run(
        self,
        *args: str,
        expected_returncode: int = 0,
        json_output: bool = True,
    ) -> Any:
        """Execute a ``gh`` command and return its parsed JSON output.

        The command must produce ``--json`` output when ``json_output=True``;
        the tool caller is responsible for selecting the fields it needs.

        When ``json_output=False`` (e.g. for ``gh --version``), returns a
        dict with a ``stdout`` key containing the raw text.
        """

        cmd = ["gh", *args]
        logger.debug("Running gh command: %s", shlex.join(cmd))
        started = perf_counter()

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"gh command timed out after 30s: {shlex.join(cmd)}") from exc

        duration_ms = (perf_counter() - started) * 1000

        if result.returncode != expected_returncode:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                f"gh command failed (exit {result.returncode}): {stderr or 'no stderr output'}"
            )

        stdout = (result.stdout or "").strip()
        if not json_output:
            return {"stdout": stdout, "_duration_ms": duration_ms}

        if not stdout:
            return {}

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh returned non-JSON output: {stdout[:200]}") from exc

        return _normalize(data, duration_ms)

    def clamp_max_results(self, requested: int | None) -> int:
        """Clamp per_page to configured limits."""

        value = requested if requested is not None else self.settings.default_max_results
        if value < 0:
            raise ValueError("per_page must be zero or greater")
        return int(min(value, self.settings.hard_max_results))


def _normalize(value: Any, duration_ms: float) -> Any:
    """Normalize gh JSON output to JSON-safe values, adding timing metadata."""

    if isinstance(value, Mapping):
        normalized = {str(k): to_json_value(v) for k, v in value.items()}
        normalized["_duration_ms"] = duration_ms
        return normalized

    if isinstance(value, list):
        return [to_json_value(item) for item in value]

    return to_json_value(value)
