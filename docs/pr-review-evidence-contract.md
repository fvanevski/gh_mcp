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

## Exact-head thread-detail workflow

Issue #83 keeps `gh_get_pr_review_state` compact and adds `gh_get_pr_review_thread` for the
narrow case where one returned thread needs comment-body context. The detail tool accepts only
the canonical repository/PR identity, one exact expected PR head SHA, one opaque thread node
ID, and bounded comment/body limits. It accepts no caller-supplied GraphQL, REST path, URL,
query fragment, cursor, or mutation control.

The server reads the PR identity before requesting thread detail, requires the current head to
match `expected_head_sha`, resolves exactly `node(id: thread_id)`, and then proves from the
returned `PullRequestReviewThread` that both `repository.nameWithOwner` and
`pullRequest.number` match the caller's requested target. The returned node ID must also equal
the requested opaque ID. Missing/inaccessible nodes, wrong node types, repository mismatch,
PR mismatch, and node-ID mismatch fail distinctly before detail can be represented as valid.
After collecting the bounded thread/comment result, the server re-reads PR base/head identity;
any movement discards the collected detail and returns `exact_head_evidence=false`.

The opaque thread ID is bound to the GraphQL variable with `gh api -f` / `--raw-field`, not
`-F` / `--field`. This distinction is security-significant: typed `-F` treats a value beginning
with `@` as a filename and reads local file contents before constructing the request. An opaque
caller-controlled thread ID must remain a literal string even when it begins with `@`; the
read-only review surface must never become an implicit local-file read primitive.

Comment ordering is the order supplied by GitHub's bounded `comments(first: ...)` connection.
Connection completeness and body completeness are independent evidence dimensions:

- `comments_evidence` reports total/returned counts, `has_more`, `truncated`, and an explicit
  warning when the requested/server comment bound leaves additional comments;
- each returned body is processed through the shared UTF-8 bounded-text helper, so
  `body_bytes_returned`, `body_total_bytes`, `body_truncated`, `body_sha256`, and
  `body_warning` describe that comment independently; and
- `body_sha256` and `body_total_bytes` cover the complete body supplied by GitHub before the
  UTF-8-safe returned prefix is truncated.

A stable PR head can therefore produce `exact_head_evidence=true` while comments or bodies
are intentionally bounded; callers must inspect the independent completeness fields rather
than equating exact identity with complete discussion content. The aggregate
`gh_get_pr_review_state` contract remains unchanged and continues to omit comment bodies.

## Formal review identity contract

Issue #75 separates three authorities that must not be conflated:

- the **PR author** is the GitHub actor recorded on the pull request;
- the **ordinary GitHub identity** is the deployment credential used by ordinary repository tools;
- the **reviewer principal** is an independently configured credential used only by
  `gh_approve_pr` and `gh_request_pr_changes`;
- **Central review authority** is the external reasoning/decision authority and is not
  itself a GitHub account.

`gh_get_pr_review_eligibility` is a read-only advisory preflight. It binds its
conclusions to `expected_head_sha`, reports the current author/ordinary/reviewer identities,
and reports whether independent GitHub approval or an ordinary-principal `COMMENTED`
review is currently available. A head movement discards the eligibility conclusion.
Write-side identity, policy, and exact-head checks are repeated independently.

### Reviewer GitHub App

The preferred reviewer principal is a GitHub App installation configured with:

```dotenv
MCP_GH_REVIEWER_APP_ID=...
MCP_GH_REVIEWER_INSTALLATION_ID=...
MCP_GH_REVIEWER_PRIVATE_KEY_FILE=/absolute/path/to/reviewer-app.pem
MCP_GH_REVIEWER_LOGIN=my-reviewer[bot]
```

The App should receive only repository access needed for the review target and **Pull
requests: Read and write** permission. App JWT signing requires the deployment `openssl`
executable. The server authenticates App-JWT bootstrap calls with `Authorization: Bearer`,
verifies the configured installation belongs to the exact target repository and still has
`pull_requests=write`, and does not mint an installation token during the read-only
eligibility call.

For a reviewer-principal write, the server mints one short-lived installation token narrowed
to the target repository and `pull_requests=write`, verifies the authenticated reviewer
actor against `expected_reviewer_login`, re-reads the PR head after reviewer authentication,
and only then attempts the formal review POST. Reviewer credentials are deployment state;
no public tool accepts a token, App ID, installation ID, private-key path, credential
selector, or alternate-auth parameter.

`MCP_GH_REVIEWER_TOKEN` is a reviewer-only staged-rollout compatibility path. It remains
independent from `GITHUB_TOKEN` but is weaker than the GitHub App path because it is a
long-lived static credential.

### Action-specific formal review writes

The public formal-review mutation surface is exactly:

- `gh_approve_pr` -> GitHub review state `APPROVED`;
- `gh_request_pr_changes` -> GitHub review state `CHANGES_REQUESTED`;
- `gh_comment_pr_review` -> GitHub review state `COMMENTED`.

`gh_submit_pr_review` is retired from the current public MCP inventory. There is no public
compatibility alias and no caller-supplied action enum that can multiplex these effects.

Every review write is bound to an exact head, attempts the review mutation at most once, and
uses immutable review-ID readback to verify review state, author, commit ID, and body. An
ambiguous transport outcome is never blindly replayed.

`gh_approve_pr` additionally requires `expected_reviewer_login` as a compare-only
precondition and rejects reviewer==PR-author before the review POST. The value cannot select
a credential.

### Same-author positive disposition

A PR author cannot create a genuine GitHub approval of their own PR. When Central review is
positive but no independent reviewer principal can legitimately approve, callers may
explicitly use `gh_comment_pr_review`. The resulting GitHub state is `COMMENTED`, even if its
body records an external/Central disposition of `APPROVE`. The server never silently converts
an approval request to a comment review, and a `COMMENTED` review must never be counted or
reported as satisfying a GitHub `APPROVED` requirement.

Whether an App-authored `APPROVED` review satisfies a particular branch/ruleset required-review
policy is separate live evidence. Do not infer policy satisfaction from the existence of the
review object; verify the configured repository policy on a disposable/live target.

### Host interception boundary

Action-specific names and schemas make the requested external effect legible to an MCP host,
but they do not guarantee that a host will route a write. If a host rejects a dedicated formal
review before server reachability, do not weaken annotations or add confirmation,
authorization, generic command/API, or credential-selector parameters. The operational
fallback is a local execution agent invoking the already-specified dedicated operation with
the configured reviewer principal, followed by Central exact-ID/head readback. Merge remains
a separate explicitly authorized operation.

GitHub references:

- https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation
- https://docs.github.com/en/rest/pulls/reviews
