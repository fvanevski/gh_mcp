"""Shared MCP composition, annotations, validation, and write/readback helpers."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_types import ToolAnnotations

from . import __version__
from .evidence import bound_text_evidence
from .gh_client import GhClient
from .settings import Settings, get_settings

READ_EXTERNAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
READ_LOCAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
ADD_EXTERNAL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
MUTATE_EXTERNAL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
OBJECT_SHA_RE = re.compile(r"^[0-9A-Fa-f]{40}$")

# Preserve the historical logger name after tool implementations move modules.
logger = logging.getLogger("mcp_gh_server.server")


@dataclass(slots=True)
class AppContext:
    client: GhClient
    settings: Settings


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    del server
    settings = get_settings()
    client = GhClient(settings=settings)
    try:
        yield AppContext(client=client, settings=settings)
    finally:
        pass  # No persistent resources to clean up


mcp = MCPServer(
    "GitHub CLI",
    instructions=(
        "Interact with GitHub via the ``gh`` CLI. Prefer catalog tools before "
        "writing. Use search tools for discovery. Read complete files before changing "
        "them. Use write tools (including atomic content commits) only when a GitHub "
        "change is necessary; they are disabled "
        "unless explicitly enabled. Approval is handled by the MCP host; the server "
        "also enforces deployment repository and operation policy."
    ),
    lifespan=app_lifespan,
    version=__version__,
)


def app_from_context(ctx: Context[AppContext]) -> AppContext:
    return ctx.request_context.lifespan_context


def configured_values(raw: str) -> set[str]:
    return {value.strip().casefold() for value in raw.split(",") if value.strip()}


def validate_repository(owner: str, repo: str) -> None:
    if not OWNER_RE.fullmatch(owner) or not REPO_RE.fullmatch(repo) or repo in {".", ".."}:
        raise ValueError("owner and repo must be canonical GitHub names without path separators")


def _configured_repository_targets(raw: str, *, env_name: str) -> set[str]:
    """Parse a comma-separated exact owner/repo allowlist and fail closed if malformed."""

    targets: set[str] = set()
    for raw_value in raw.split(","):
        value = raw_value.strip()
        if not value:
            continue
        if value.count("/") != 1:
            raise RuntimeError(f"{env_name} contains invalid repository target {value!r}")
        owner, repo = value.split("/", 1)
        try:
            validate_repository(owner, repo)
        except ValueError as exc:
            raise RuntimeError(
                f"{env_name} contains invalid repository target {value!r}"
            ) from exc
        targets.add(f"{owner}/{repo}".casefold())
    return targets


def _normalize_workflow_selector(workflow: int | str) -> str:
    """Normalize a positive workflow ID or preserve one exact configured workflow path."""

    if isinstance(workflow, bool):
        raise ValueError("workflow selector must be a positive ID or exact workflow path")
    if isinstance(workflow, int):
        if workflow < 1:
            raise ValueError("workflow selector must be a positive ID or exact workflow path")
        return str(workflow)

    value = workflow.strip()
    if (
        not value
        or value != workflow
        or len(value.encode()) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("workflow selector must be a positive ID or exact workflow path")
    if value.isdecimal():
        workflow_id = int(value)
        if workflow_id < 1:
            raise ValueError("workflow selector must be a positive ID or exact workflow path")
        return str(workflow_id)
    return value


def _configured_workflow_targets(raw: str, *, env_name: str) -> set[tuple[str, str]]:
    """Parse exact owner/repo@workflow targets while preserving workflow-path case."""

    targets: set[tuple[str, str]] = set()
    for raw_value in raw.split(","):
        value = raw_value.strip()
        if not value:
            continue
        repository, separator, workflow = value.partition("@")
        if not separator or repository.count("/") != 1:
            raise RuntimeError(f"{env_name} contains invalid workflow target {value!r}")
        owner, repo = repository.split("/", 1)
        try:
            validate_repository(owner, repo)
            selector = _normalize_workflow_selector(workflow)
        except ValueError as exc:
            raise RuntimeError(f"{env_name} contains invalid workflow target {value!r}") from exc
        targets.add((f"{owner}/{repo}".casefold(), selector))
    return targets


def require_repo_creation_target(app: AppContext, owner: str, repo: str) -> None:
    """Require exact authorization for one prospective repository creation target."""

    target = f"{owner}/{repo}".casefold()
    allowed = _configured_repository_targets(
        app.settings.allowed_repo_creation_targets,
        env_name="MCP_GH_ALLOWED_REPO_CREATION_TARGETS",
    )
    if target not in allowed:
        raise RuntimeError(
            f"GitHub repository creation is not allowed for prospective repository {owner}/{repo}; "
            "configure MCP_GH_ALLOWED_REPO_CREATION_TARGETS"
        )


def require_workflow_dispatch_target(
    app: AppContext,
    owner: str,
    repo: str,
    workflow: int | str,
) -> None:
    """Require exact authorization for one repository/workflow dispatch target."""

    target = (f"{owner}/{repo}".casefold(), _normalize_workflow_selector(workflow))
    allowed = _configured_workflow_targets(
        app.settings.allowed_workflow_dispatch_targets,
        env_name="MCP_GH_ALLOWED_WORKFLOW_DISPATCH_TARGETS",
    )
    if target not in allowed:
        raise RuntimeError(
            f"GitHub workflow dispatch is not allowed for {owner}/{repo}@{target[1]}; "
            "configure MCP_GH_ALLOWED_WORKFLOW_DISPATCH_TARGETS"
        )


def require_write_enabled(
    app: AppContext,
    owner: str,
    repo: str,
    *,
    action: str,
    workflow: int | str | None = None,
) -> None:
    """Enforce server-side write, repository, and high-risk action policy."""

    validate_repository(owner, repo)
    require_action_enabled(app, action)

    allowed_repositories = configured_values(app.settings.allowed_repositories)
    allowed_owners = configured_values(app.settings.allowed_owners)
    target = f"{owner}/{repo}".casefold()
    if (allowed_repositories or allowed_owners) and (
        target not in allowed_repositories and owner.casefold() not in allowed_owners
    ):
        raise RuntimeError(f"GitHub writes are not allowed for repository {owner}/{repo}")

    if action == "repo_create":
        require_repo_creation_target(app, owner, repo)
    elif action == "workflow_dispatch":
        if workflow is None:
            raise RuntimeError("workflow dispatch authorization requires an exact workflow target")
        require_workflow_dispatch_target(app, owner, repo, workflow)


def require_action_enabled(app: AppContext, action: str) -> None:
    """Enforce global and high-risk operation switches before target discovery."""

    if not app.settings.allow_write_commands:
        raise RuntimeError("GitHub writes are disabled by MCP_GH_ALLOW_WRITE_COMMANDS")

    action_settings = {
        "repo_create": app.settings.allow_repo_creation,
        "release_create": app.settings.allow_release_creation,
        "workflow_dispatch": app.settings.allow_workflow_dispatch,
        "content_commit": app.settings.allow_content_commits,
        "pr_merge": app.settings.allow_pr_merge,
    }
    if action in action_settings and not action_settings[action]:
        env_name = {
            "repo_create": "MCP_GH_ALLOW_REPO_CREATION",
            "release_create": "MCP_GH_ALLOW_RELEASE_CREATION",
            "workflow_dispatch": "MCP_GH_ALLOW_WORKFLOW_DISPATCH",
            "content_commit": "MCP_GH_ALLOW_CONTENT_COMMITS",
            "pr_merge": "MCP_GH_ALLOW_PR_MERGE",
        }[action]
        raise RuntimeError(f"GitHub action {action!r} is disabled by {env_name}")


def parse_search_result(result: Any) -> tuple[list[Any], int]:
    """Parse gh search output which may be a list or a dict with 'results' key."""
    if isinstance(result, list):
        return result, 0
    if isinstance(result, dict):
        items = result.get("results", [])
        total = result.get("totalCount", 0)
        if isinstance(items, list):
            return items, total
    return [], 0


def write_stdout(result: Any, resource: str) -> str:
    """Return non-empty stdout from a successful non-JSON write command."""

    if isinstance(result, dict):
        stdout = result.get("stdout")
        if isinstance(stdout, str) and stdout.strip():
            return stdout.strip()
    raise RuntimeError(
        f"{resource} was created, but gh did not return the locator needed to read it back"
    )


def created_url(result: Any, resource: str) -> str:
    """Extract and validate the URL printed by a successful gh create command."""

    stdout = write_stdout(result, resource)
    for token in reversed(stdout.split()):
        url = token.rstrip(".,;)")
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return url
    raise RuntimeError(f"{resource} was created, but gh returned no valid resource URL")


def optional_created_url(result: Any) -> str | None:
    try:
        return created_url(result, "Resource")
    except RuntimeError:
        return None


def trailing_number(url: str | None) -> int:
    if not url:
        return 0
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return 0


def readback_warning(resource: str, locator: str | None = None) -> str:
    location = f" at {locator}" if locator else ""
    return (
        f"{resource} write completed{location}, but structured readback failed. "
        "Do not retry automatically; verify the resource first."
    )


def created_json(result: Any, resource: str) -> dict[str, Any]:
    """Parse the response body from a successful gh api write command."""

    raw = write_stdout(result, resource)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{resource} was created, but gh returned a non-JSON API response"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{resource} was created, but gh returned an unexpected API response")
    return parsed


async def get_label(client: GhClient, owner: str, repo: str, name: str) -> dict[str, Any]:
    """Read a label after a write using its exact REST resource path."""

    result = await client.run(
        "api",
        f"repos/{owner}/{repo}/labels/{quote(name, safe='')}",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"gh returned an unexpected response while reading label {name!r}")
    return result


def validate_repo_path(path: str) -> None:
    parts = path.split("/")
    if (
        not path
        or len(path.encode()) > 4096
        or path.startswith("/")
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.casefold() == ".git" for part in parts)
    ):
        raise ValueError(f"invalid repository-relative file path: {path!r}")


def validate_branch(branch: str) -> None:
    invalid = re.search(r"[\x00-\x20~^:?*\[]", branch)
    if (
        not branch
        or len(branch.encode()) > 1024
        or branch == "@"
        or branch.startswith("/")
        or branch.endswith("/")
        or branch.endswith(".")
        or branch.endswith(".lock")
        or ".." in branch
        or "@{" in branch
        or "//" in branch
        or "\\" in branch
        or invalid is not None
    ):
        raise ValueError("branch is not a valid Git branch name")


async def api_json_write(
    client: GhClient,
    method: str,
    endpoint: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send a JSON GitHub API write without exposing its body in argv or logs."""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as file:
        json.dump(payload, file)
        file.flush()
        payload_path = file.name
    try:
        result = await client.run(
            "api",
            endpoint,
            "-X",
            method,
            "--input",
            payload_path,
        )
    finally:
        os.unlink(payload_path)
    if not isinstance(result, dict):
        raise RuntimeError(f"GitHub API {method} {endpoint} returned a non-object response")
    return result


def workflow_run_id(url: str) -> int:
    """Extract a workflow run identifier from its URL."""

    parts = [part for part in urlparse(url).path.split("/") if part]
    try:
        runs_index = parts.index("runs")
        return int(parts[runs_index + 1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(
            f"Workflow was dispatched, but its run URL is malformed: {url!r}"
        ) from exc


def bounded_utf8(content: str, limit: int) -> tuple[str, int, int, bool, str]:
    """Compatibility projection of the shared bounded-text evidence contract."""

    evidence = bound_text_evidence(
        content,
        requested_max_bytes=limit,
        hard_max_bytes=limit,
    )
    return (
        evidence.content,
        evidence.bytes_returned,
        evidence.total_bytes,
        evidence.truncated,
        evidence.sha256,
    )
