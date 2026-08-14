"""Canonical exact-outcome issue-linked branch write implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import Context
from pydantic import Field

from ..git_write_models import BranchCreate
from ..request_governor import GitHubRequestError, GitHubRequestResult
from ..tooling import (
    OBJECT_SHA_RE,
    OWNER_RE,
    REPO_RE,
    AppContext,
    app_from_context,
    logger,
    require_write_enabled,
    validate_branch,
)
from ..write_contracts import (
    WritePrecondition,
    execute_write_readback,
    require_write_precondition,
    run_api_json_write_with_metadata,
)

_LINKED_BRANCH_PAGE_SIZE = 100
_MAX_LINKED_BRANCH_PAGES = 10
_LINKED_BRANCH_QUERY = """
query IssueLinkedBranches(
  $owner: String!
  $repo: String!
  $number: Int!
  $first: Int!
  $after: String
) {
  repository(owner: $owner, name: $repo) {
    id
    issue(number: $number) {
      id
      linkedBranches(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          ref { name prefix repository { id } target { oid } }
        }
      }
    }
  }
}
""".strip()
_CREATE_LINKED_BRANCH_MUTATION = """
mutation CreateLinkedBranch($input: CreateLinkedBranchInput!) {
  createLinkedBranch(input: $input) {
    issue { id }
    linkedBranch { id ref { name prefix target { oid } } }
  }
}
""".strip()


@dataclass(frozen=True, slots=True)
class _LinkedBranchReadback:
    linked_branch_id: str | None
    repository_id: str | None
    ref: str | None
    sha: str | None


async def _read_exact_branch_sha(
    app: AppContext,
    owner: str,
    repo: str,
    branch: str,
) -> str:
    branch_path = quote(branch, safe="/")
    result = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/git/ref/heads/{branch_path}",
        "-X",
        "GET",
    )
    if not isinstance(result, dict) or result.get("ref") != f"refs/heads/{branch}":
        raise RuntimeError(f"GitHub did not return the exact branch ref for {branch!r}")
    obj = result.get("object")
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not OBJECT_SHA_RE.fullmatch(sha):
        raise RuntimeError(f"GitHub did not return the exact branch head SHA for {branch!r}")
    return sha.casefold()


def _parse_linked_branch(node: object) -> _LinkedBranchReadback:
    if not isinstance(node, dict):
        raise RuntimeError("GitHub returned a malformed linked-branch node")
    linked_branch_id = node.get("id")
    ref = node.get("ref")
    if not isinstance(linked_branch_id, str) or not linked_branch_id:
        raise RuntimeError("GitHub returned a linked branch without a stable node ID")
    if not isinstance(ref, dict):
        raise RuntimeError("GitHub returned a linked branch without a ref")
    name = ref.get("name")
    prefix = ref.get("prefix")
    repository = ref.get("repository")
    repository_id = repository.get("id") if isinstance(repository, dict) else None
    target = ref.get("target")
    sha = target.get("oid") if isinstance(target, dict) else None
    if not isinstance(name, str) or prefix != "refs/heads/":
        raise RuntimeError("GitHub returned a linked branch with a malformed branch ref")
    if not isinstance(repository_id, str) or not repository_id:
        raise RuntimeError("GitHub returned a linked branch without repository identity")
    if not isinstance(sha, str) or not OBJECT_SHA_RE.fullmatch(sha):
        raise RuntimeError("GitHub returned a linked branch without an exact target SHA")
    return _LinkedBranchReadback(
        linked_branch_id=linked_branch_id,
        repository_id=repository_id,
        ref=f"{prefix}{name}",
        sha=sha.casefold(),
    )


async def _read_issue_linked_branch(
    app: AppContext,
    owner: str,
    repo: str,
    issue_number: int,
    *,
    repository_node_id: str,
    issue_node_id: str,
    branch_ref: str,
) -> _LinkedBranchReadback:
    after: str | None = None
    matching: list[_LinkedBranchReadback] = []
    for _ in range(_MAX_LINKED_BRANCH_PAGES):
        args = [
            "api",
            "graphql",
            "-f",
            f"query={_LINKED_BRANCH_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo}",
            "-F",
            f"number={issue_number}",
            "-F",
            f"first={_LINKED_BRANCH_PAGE_SIZE}",
        ]
        if after is not None:
            args.extend(["-F", f"after={after}"])
        result = await app.client.run(*args)
        if not isinstance(result, dict) or result.get("errors"):
            raise RuntimeError("GitHub GraphQL linked-branch readback failed")
        data = result.get("data")
        repository = data.get("repository") if isinstance(data, dict) else None
        if not isinstance(repository, dict) or repository.get("id") != repository_node_id:
            raise RuntimeError("GitHub GraphQL did not preserve exact repository identity")
        issue = repository.get("issue")
        if not isinstance(issue, dict) or issue.get("id") != issue_node_id:
            raise RuntimeError("GitHub GraphQL did not preserve exact issue identity")
        connection = issue.get("linkedBranches")
        if not isinstance(connection, dict):
            raise RuntimeError("GitHub GraphQL returned no linked-branch connection")
        nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise RuntimeError("GitHub returned malformed linked-branch pagination evidence")
        if len(nodes) > _LINKED_BRANCH_PAGE_SIZE:
            raise RuntimeError("GitHub exceeded the linked-branch readback page bound")
        for node in nodes:
            parsed = _parse_linked_branch(node)
            if parsed.ref == branch_ref and parsed.repository_id == repository_node_id:
                matching.append(parsed)
        if len(matching) > 1:
            raise RuntimeError("GitHub returned multiple issue-linked branches for one exact ref")
        has_next_page = page_info.get("hasNextPage")
        end_cursor = page_info.get("endCursor")
        if not isinstance(has_next_page, bool):
            raise RuntimeError("GitHub returned malformed linked-branch page information")
        if not has_next_page:
            return matching[0] if matching else _LinkedBranchReadback(None, None, None, None)
        if not isinstance(end_cursor, str) or not end_cursor:
            raise RuntimeError("GitHub linked-branch pagination omitted the next cursor")
        after = end_cursor
    raise RuntimeError(
        "Issue linked-branch readback exceeded the bounded 1000-branch evidence limit"
    )


async def gh_create_branch(
    owner: Annotated[str, Field(min_length=1, max_length=39, pattern=OWNER_RE.pattern)],
    repo: Annotated[str, Field(min_length=1, max_length=100, pattern=REPO_RE.pattern)],
    issue_number: Annotated[int, Field(ge=1)],
    name: Annotated[str, Field(min_length=1, max_length=1024)],
    *,
    ctx: Context[AppContext],
    base: Annotated[str | None, Field(min_length=1, max_length=1024)] = None,
) -> BranchCreate:
    """Create one exact issue-linked branch with authoritative association readback."""

    logger.info("MCP tool invocation reached server: tool=gh_create_branch")
    app = app_from_context(ctx)
    require_write_enabled(app, owner, repo, action="branch_create")
    validate_branch(name)
    if base is not None:
        if OBJECT_SHA_RE.fullmatch(base):
            raise ValueError(
                "base accepts branch names only; use gh_create_branch_from_sha "
                "with base_sha for an immutable commit base"
            )
        validate_branch(base)

    repository = await app.client.run("api", f"repos/{owner}/{repo}", "-X", "GET")
    if not isinstance(repository, dict):
        raise RuntimeError("GitHub did not return repository identity for linked-branch creation")
    repository_node_id = repository.get("node_id")
    default_branch = repository.get("default_branch")
    if not isinstance(repository_node_id, str) or not repository_node_id:
        raise RuntimeError("GitHub did not return the repository node ID")
    if not isinstance(default_branch, str) or not default_branch:
        raise RuntimeError("GitHub did not return the repository default branch")

    issue = await app.client.run(
        "api",
        f"repos/{owner}/{repo}/issues/{issue_number}",
        "-X",
        "GET",
    )
    if not isinstance(issue, dict) or "pull_request" in issue:
        raise ValueError(f"#{issue_number} is not an issue eligible for a linked branch")
    issue_node_id = issue.get("node_id")
    if not isinstance(issue_node_id, str) or not issue_node_id:
        raise RuntimeError("GitHub did not return the issue node ID")

    base_branch = base or default_branch
    validate_branch(base_branch)
    expected_base_sha = await _read_exact_branch_sha(app, owner, repo, base_branch)
    branch_ref = f"refs/heads/{name}"

    async def precondition() -> WritePrecondition[str]:
        return await require_write_precondition(
            lambda: _read_exact_branch_sha(app, owner, repo, base_branch),
            expected_base_sha,
            label="Issue branch base ref",
        )

    async def write() -> GitHubRequestResult[Any]:
        result = await run_api_json_write_with_metadata(
            app.client,
            "POST",
            "graphql",
            {
                "query": _CREATE_LINKED_BRANCH_MUTATION,
                "variables": {
                    "input": {
                        "issueId": issue_node_id,
                        "repositoryId": repository_node_id,
                        "oid": expected_base_sha,
                        "name": name,
                    }
                },
            },
        )
        value = result.value
        errors = value.get("errors") if isinstance(value, dict) else None
        if errors:
            raise GitHubRequestError(
                "GitHub GraphQL returned mutation errors during linked-branch creation",
                ambiguous=True,
                metadata=result.metadata,
            )
        return result

    async def readback() -> _LinkedBranchReadback:
        return await _read_issue_linked_branch(
            app,
            owner,
            repo,
            issue_number,
            repository_node_id=repository_node_id,
            issue_node_id=issue_node_id,
            branch_ref=branch_ref,
        )

    execution = await execute_write_readback(
        resource="Issue development branch",
        precondition=precondition,
        write=write,
        readback=readback,
        state_matches_requested=lambda value: (
            value.repository_id == repository_node_id
            and value.ref == branch_ref
            and value.sha == expected_base_sha
        ),
    )
    outcome = execution.outcome
    readback_value = execution.readback_value
    if (
        execution.error is not None
        and outcome.write_completed is False
        and outcome.state_matches_requested is not True
    ):
        raise execution.error

    created = outcome.write_completed is True
    linked_branch_id = readback_value.linked_branch_id if readback_value is not None else None
    if outcome.write_completed is False and outcome.state_matches_requested is True:
        message = (
            f"Branch '{name}' is already linked to issue #{issue_number} at the requested "
            "base commit; no write was performed."
        )
    elif outcome.warning is not None:
        message = outcome.warning
    else:
        message = f"Branch '{name}' created and linked to issue #{issue_number}."

    return BranchCreate(
        name=name,
        ref=branch_ref,
        base_sha=expected_base_sha,
        linked_branch_id=linked_branch_id,
        created=created,
        message=message,
        **outcome.model_dump(),
    )
