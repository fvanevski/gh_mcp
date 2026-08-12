# Pull-request review evidence contracts

This note records the authority and completeness rules used by the exact-head PR review tools introduced for issue #19.

## GraphQL request authority

GitHub GraphQL queries and mutations both use HTTP `POST` when a request body is supplied. HTTP method alone therefore cannot distinguish a read from a mutation.

`GhClient` treats a GraphQL POST as a retry-safe read only when all of the following are true:

- the `gh api` endpoint is exactly `graphql`;
- the query document is supplied directly through one explicit `query=` field;
- no `--input` body source is present;
- the document begins with an explicit `query` operation, allowing leading GraphQL line comments;
- the document contains no `mutation` or `subscription` operation token; and
- the effective HTTP method is `POST`.

All ambiguous forms fail closed as write-classified requests. This includes mutations, subscriptions, mixed query/mutation documents, anonymous query documents, indirect `query=@file` input, `--input` request bodies, malformed or duplicate query fields, and non-POST method overrides. False negatives are acceptable because they retain conservative write semantics; false-positive read classification is not.

This distinction matters because the request governor gives proven reads bounded retry semantics, while writes are non-retryable, write-spaced, and may be reported as transport-ambiguous.

Regression coverage lives in `tests/test_graphql_request_policy.py` and exercises both request classification and the real `GhClient` subprocess/governor boundary.

GitHub references:

- https://docs.github.com/en/graphql/guides/forming-calls-with-graphql
- https://cli.github.com/manual/gh_api

## Requested-reviewer completeness

`gh_get_pr_review_state` reads outstanding requested users and teams from GitHub's REST endpoint:

`GET /repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers`

GitHub documents this operation as **Get all requested reviewers for a pull request**. Unlike the paginated `List reviews for a pull request` endpoint, the requested-reviewers operation exposes no `page` or `per_page` query parameters in the documented contract. The implementation therefore must not invent pagination or claim a server-side page bound that GitHub does not expose.

If GitHub later changes this endpoint to a paginated/bounded contract, `exact_head_evidence` must be revised at the same time so incomplete requested-reviewer evidence cannot be treated as complete.

GitHub references:

- https://docs.github.com/en/rest/pulls/review-requests
- https://docs.github.com/en/rest/pulls/reviews

## Exact-head aggregate invariant

The aggregate remains valid only while the pull request's base and head SHAs stay unchanged across the multi-request read. A head mismatch, base/head movement during the read, truncated review evidence, or truncated review-thread evidence prevents a satisfied exact-head conclusion.
