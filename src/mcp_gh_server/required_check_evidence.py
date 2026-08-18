"""Exact-head integration identity evidence for required GitHub checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .merge_requirements_models import RequiredStatusCheckObservation
from .tooling import AppContext

_GITHUB_CHECK_CONTEXTS_PAGE_MAX = 100
_REQUIRED_CHECK_IDENTITIES_QUERY = """
query PullRequestRequiredCheckIdentities(
  $owner: String!
  $repo: String!
  $number: Int!
  $first: Int!
  $after: String
) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      baseRefOid
      headRefOid
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              contexts(first: $first, after: $after) {
                nodes {
                  __typename
                  ... on StatusContext {
                    context
                    isRequired(pullRequestNumber: $number)
                  }
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    startedAt
                    completedAt
                    detailsUrl
                    isRequired(pullRequestNumber: $number)
                    checkSuite {
                      app {
                        databaseId
                      }
                    }
                  }
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
          }
        }
      }
    }
  }
}
""".strip()


@dataclass(slots=True)
class PinnedRequiredCheckRead:
    """Integration-pinned exact-head observations plus completeness evidence."""

    checks: list[RequiredStatusCheckObservation]
    complete: bool
    truncated: bool
    identity_matches: bool
    warnings: list[str]


def _optional_string(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"GitHub returned malformed {label}")
    return value


def _bucket(state: str) -> Literal["pass", "fail", "pending", "skipping", "cancel"]:
    if state == "SUCCESS":
        return "pass"
    if state in {"SKIPPED", "NEUTRAL"}:
        return "skipping"
    if state in {"ERROR", "FAILURE", "TIMED_OUT", "ACTION_REQUIRED"}:
        return "fail"
    if state == "CANCELLED":
        return "cancel"
    return "pending"


def _required_status_context(payload: Any) -> str | None:
    """Return a required legacy status context, which cannot carry GitHub App identity."""

    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned a malformed required-check node")
    if payload.get("__typename") != "StatusContext":
        return None
    is_required = payload.get("isRequired")
    if not isinstance(is_required, bool):
        raise RuntimeError("GitHub returned a status context without isRequired")
    if not is_required:
        return None
    context = payload.get("context")
    if not isinstance(context, str) or not context:
        raise RuntimeError("GitHub returned a required status context without a valid context")
    return context


def _check_run_observation(payload: Any) -> RequiredStatusCheckObservation | None:
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned a malformed required-check node")
    if payload.get("__typename") != "CheckRun":
        return None

    is_required = payload.get("isRequired")
    if not isinstance(is_required, bool):
        raise RuntimeError("GitHub returned a required-check node without isRequired")
    if not is_required:
        return None

    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("GitHub returned a required check without a valid name")

    check_suite = payload.get("checkSuite")
    if not isinstance(check_suite, dict):
        raise RuntimeError("GitHub returned a required check without check-suite identity")
    app = check_suite.get("app")
    if app is not None and not isinstance(app, dict):
        raise RuntimeError("GitHub returned a required check with malformed app identity")
    integration_id = app.get("databaseId") if isinstance(app, dict) else None
    if integration_id is not None and (
        not isinstance(integration_id, int)
        or isinstance(integration_id, bool)
        or integration_id < 1
    ):
        raise RuntimeError("GitHub returned a required check with invalid app database id")

    status = payload.get("status")
    conclusion = payload.get("conclusion")
    if status is not None and not isinstance(status, str):
        raise RuntimeError("GitHub returned a required check with malformed status")
    if conclusion is not None and not isinstance(conclusion, str):
        raise RuntimeError("GitHub returned a required check with malformed conclusion")
    state = conclusion if status == "COMPLETED" and conclusion else status
    state = state or "UNKNOWN"

    return RequiredStatusCheckObservation(
        name=name,
        integration_id=integration_id,
        state=state,
        bucket=_bucket(state),
        started_at=_optional_string(payload.get("startedAt"), label="required-check start time"),
        completed_at=_optional_string(
            payload.get("completedAt"), label="required-check completion time"
        ),
        link=_optional_string(payload.get("detailsUrl"), label="required-check details URL"),
    )


def _page_payload(
    payload: Any,
    *,
    expected_base_sha: str,
    expected_head_sha: str,
) -> tuple[list[Any], bool, str | None, bool]:
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub did not return structured required-check identity evidence")
    if payload.get("errors"):
        raise RuntimeError("GitHub GraphQL returned errors while reading required-check identity")

    data = payload.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(pull_request, dict):
        raise RuntimeError("GitHub GraphQL did not return the requested pull request")

    base_sha = pull_request.get("baseRefOid")
    head_sha = pull_request.get("headRefOid")
    identity_matches = (
        isinstance(base_sha, str)
        and isinstance(head_sha, str)
        and (base_sha.casefold(), head_sha.casefold())
        == (expected_base_sha.casefold(), expected_head_sha.casefold())
    )

    commits = pull_request.get("commits")
    commit_nodes = commits.get("nodes") if isinstance(commits, dict) else None
    if not isinstance(commit_nodes, list) or len(commit_nodes) != 1:
        raise RuntimeError("GitHub did not return the exact pull-request head commit")
    commit_wrapper = commit_nodes[0]
    commit = commit_wrapper.get("commit") if isinstance(commit_wrapper, dict) else None
    if not isinstance(commit, dict):
        raise RuntimeError("GitHub returned malformed exact-head commit evidence")
    commit_oid = commit.get("oid")
    if not isinstance(commit_oid, str):
        raise RuntimeError("GitHub returned malformed exact-head commit identity")
    identity_matches &= commit_oid.casefold() == expected_head_sha.casefold()

    rollup = commit.get("statusCheckRollup")
    if rollup is None:
        return [], False, None, identity_matches
    if not isinstance(rollup, dict):
        raise RuntimeError("GitHub returned malformed status-check rollup evidence")
    contexts = rollup.get("contexts")
    if not isinstance(contexts, dict):
        raise RuntimeError("GitHub returned malformed status-check context evidence")
    nodes = contexts.get("nodes")
    page_info = contexts.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise RuntimeError("GitHub returned malformed status-check pagination evidence")
    has_next = page_info.get("hasNextPage")
    end_cursor = page_info.get("endCursor")
    if not isinstance(has_next, bool):
        raise RuntimeError("GitHub returned malformed status-check page information")
    if has_next and (not isinstance(end_cursor, str) or not end_cursor):
        raise RuntimeError("GitHub omitted the next status-check cursor")
    if not has_next and end_cursor is not None and not isinstance(end_cursor, str):
        raise RuntimeError("GitHub returned malformed status-check cursor")
    return nodes, has_next, end_cursor, identity_matches


async def read_pinned_required_check_evidence(
    app: AppContext,
    owner: str,
    repo: str,
    number: int,
    *,
    base_sha: str,
    head_sha: str,
    required_identities: set[tuple[str, int]],
    limit: int,
) -> PinnedRequiredCheckRead:
    """Read app identity for every integration-pinned required check at one PR head."""

    if not required_identities:
        return PinnedRequiredCheckRead([], True, False, True, [])

    bounded_limit = max(1, min(limit, 1_000))
    pinned_contexts = {context for context, _integration_id in required_identities}
    observations: dict[tuple[str, int], RequiredStatusCheckObservation] = {}
    warnings: list[str] = []
    seen_nodes = 0
    after: str | None = None
    identity_matches = True

    while True:
        first = min(_GITHUB_CHECK_CONTEXTS_PAGE_MAX, bounded_limit - seen_nodes)
        if first < 1:
            warnings.append(
                "Exact-head required-check identity evidence exceeds the configured result bound."
            )
            return PinnedRequiredCheckRead(
                list(observations.values()),
                False,
                True,
                identity_matches,
                warnings,
            )

        args = [
            "api",
            "graphql",
            "-f",
            f"query={_REQUIRED_CHECK_IDENTITIES_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo}",
            "-F",
            f"number={number}",
            "-F",
            f"first={first}",
        ]
        if after is not None:
            args.extend(["-F", f"after={after}"])
        payload = await app.client.run(*args)
        nodes, has_next, end_cursor, page_identity_matches = _page_payload(
            payload,
            expected_base_sha=base_sha,
            expected_head_sha=head_sha,
        )
        identity_matches &= page_identity_matches
        if len(nodes) > first:
            raise RuntimeError(
                "GitHub returned more status-check contexts than the requested page bound"
            )
        seen_nodes += len(nodes)

        for node in nodes:
            status_context = _required_status_context(node)
            if status_context is not None and status_context in pinned_contexts:
                warnings.append(
                    f"GitHub reports required status context {status_context!r}, which has no "
                    "GitHub App identity; integration-pinned check evidence is incomplete."
                )
                continue

            observation = _check_run_observation(node)
            if observation is None or observation.name not in pinned_contexts:
                continue
            integration_id = observation.integration_id
            if integration_id is None:
                warnings.append(
                    f"GitHub reports required check {observation.name!r} without app identity; "
                    "integration-pinned check evidence is incomplete."
                )
                continue
            key = (observation.name, integration_id)
            if key not in required_identities:
                warnings.append(
                    f"GitHub reports required check {observation.name!r} from app "
                    f"{integration_id}, which is absent from the composed required-check policy."
                )
                continue
            previous = observations.get(key)
            if previous is None or (observation.started_at or "") > (previous.started_at or ""):
                observations[key] = observation

        if not has_next:
            break
        if seen_nodes >= bounded_limit:
            warnings.append(
                "Exact-head required-check identity evidence exceeds the configured result bound."
            )
            return PinnedRequiredCheckRead(
                list(observations.values()),
                False,
                True,
                identity_matches,
                warnings,
            )
        after = end_cursor

    complete = identity_matches and not warnings
    return PinnedRequiredCheckRead(
        list(observations.values()),
        complete,
        False,
        identity_matches,
        warnings,
    )
