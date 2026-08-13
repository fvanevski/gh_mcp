"""Focused response-header provenance tests for governed API reads."""

from mcp_gh_server.binary_evidence import _metadata_from_headers


def test_binary_evidence_captures_complete_primary_rate_headers() -> None:
    metadata = _metadata_from_headers(
        {
            "x-github-request-id": "req-search",
            "x-ratelimit-resource": "search",
            "x-ratelimit-limit": "30",
            "x-ratelimit-remaining": "29",
            "x-ratelimit-used": "1",
            "x-ratelimit-reset": "2400",
        },
        not_modified=False,
    )

    assert metadata.request_id == "req-search"
    assert metadata.rate_limit_resource == "search"
    assert metadata.rate_limit_limit == 30
    assert metadata.rate_limit_remaining == 29
    assert metadata.rate_limit_used == 1
    assert metadata.rate_limit_reset_epoch == 2400
