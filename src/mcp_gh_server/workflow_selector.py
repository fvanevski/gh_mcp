"""Canonical workflow selector validation and exact path-to-ID resolution."""

from __future__ import annotations

import re
from urllib.parse import quote

from .tooling import AppContext

WORKFLOW_PATH_RE = re.compile(r"^\.github/workflows/[^/\x00-\x1f\x7f]+\.ya?ml$")


def validate_workflow_selector(workflow: int | str) -> int | str:
    """Validate one positive workflow ID or canonical workflow file path."""

    if isinstance(workflow, bool):
        raise ValueError("workflow selector must be a positive ID or canonical workflow path")
    if isinstance(workflow, int):
        if workflow < 1:
            raise ValueError("workflow selector must be a positive ID or canonical workflow path")
        return workflow
    if (
        not isinstance(workflow, str)
        or len(workflow.encode()) > 1024
        or not WORKFLOW_PATH_RE.fullmatch(workflow)
    ):
        raise ValueError(
            "workflow selector must be a positive ID or canonical .github/workflows/*.yml path"
        )
    return workflow


async def resolve_workflow_id(
    app: AppContext,
    owner: str,
    repo: str,
    workflow: int | str,
) -> int:
    """Resolve an authorized exact workflow selector to GitHub's numeric workflow ID.

    Numeric IDs require no discovery. Canonical paths are resolved through GitHub's
    workflow-by-file endpoint and accepted only when authoritative readback returns the
    exact same case-sensitive path. Resolution is read-only and occurs before mutation.
    """

    selector = validate_workflow_selector(workflow)
    if isinstance(selector, int):
        return selector

    filename = selector.rsplit("/", 1)[-1]
    raw = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/actions/workflows/{quote(filename, safe='')}",
        "-X",
        "GET",
    )
    if not isinstance(raw, dict):
        raise RuntimeError(
            "GitHub returned non-object workflow metadata during selector resolution"
        )
    workflow_id = raw.get("id")
    path = raw.get("path")
    if not isinstance(workflow_id, int) or workflow_id < 1:
        raise RuntimeError("GitHub returned no positive workflow ID during selector resolution")
    if not isinstance(path, str) or path != selector:
        raise RuntimeError(
            f"GitHub workflow selector resolved to path {path!r}, expected exact path {selector!r}; "
            "no write was attempted"
        )
    return workflow_id
