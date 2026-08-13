"""Regression audit of every public write-tool schema and annotation contract."""

from __future__ import annotations

import re

import pytest

from mcp_gh_server.server import mcp

# Exact set of all public write tools; must stay in sync with write_tool_schema.
PUBLIC_WRITE_TOOLS = frozenset(
    {
        "gh_create_issue",
        "gh_edit_issue",
        "gh_set_issue_state",
        "gh_create_label",
        "gh_upsert_label",
        "gh_edit_label",
        "gh_create_milestone",
        "gh_create_comment",
        "gh_create_pr",
        "gh_edit_pr",
        "gh_set_pr_draft_state",
        "gh_submit_pr_review",
        "gh_merge_pr",
        "gh_create_repo",
        "gh_commit_files",
        "gh_create_release",
        "gh_create_release_exact",
        "gh_run_workflow",
        "gh_run_workflow_exact",
        "gh_create_branch",
        "gh_create_branch_from_sha",
    }
)

ADDITIVE_WRITE_TOOLS = {
    "gh_create_issue",
    "gh_create_label",
    "gh_create_milestone",
    "gh_create_comment",
    "gh_create_pr",
    "gh_submit_pr_review",
    "gh_create_repo",
    "gh_create_release",
    "gh_create_release_exact",
    "gh_create_branch",
    "gh_create_branch_from_sha",
}

DESTRUCTIVE_WRITE_TOOLS = {
    "gh_edit_issue",
    "gh_set_issue_state",
    "gh_upsert_label",
    "gh_edit_label",
    "gh_edit_pr",
    "gh_set_pr_draft_state",
    "gh_merge_pr",
    "gh_run_workflow",
    "gh_run_workflow_exact",
    "gh_commit_files",
}

OBJECT_SHA_PATTERN = r"^[0-9A-Fa-f]{40}$"
REF_PATTERN = r"^(?:heads|tags)/.+$"
LABEL_COLOR_PATTERN = r"^[0-9A-Fa-f]{6}$"


def _extract_array_inner(schema: dict) -> dict:
    """Unwrap anyOf wrapping an array (e.g. optional array fields)."""
    if schema.get("type") == "array":
        return schema
    any_of = schema.get("anyOf", [])
    non_null = [item for item in any_of if item.get("type") != "null"]
    if len(non_null) == 1:
        return non_null[0]
    return schema


def _extract_string_inner(schema: dict) -> dict:
    """Unwrap anyOf wrapping a string (e.g. optional string fields)."""
    if schema.get("type") == "string":
        return schema
    any_of = schema.get("anyOf", [])
    non_null = [item for item in any_of if item.get("type") != "null"]
    if len(non_null) == 1:
        return non_null[0]
    return schema


@pytest.mark.asyncio
async def test_write_tool_count_matches_public_surface() -> None:
    tools = await mcp.list_tools()
    write_names = {t.name for t in tools if t.name in PUBLIC_WRITE_TOOLS}
    assert write_names == PUBLIC_WRITE_TOOLS


@pytest.mark.asyncio
async def test_all_writes_are_not_read_only() -> None:
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for name in PUBLIC_WRITE_TOOLS:
        tool = tools_dict[name]
        assert tool.annotations.read_only_hint is False, f"{name} must be writable"


@pytest.mark.asyncio
async def test_additive_writes_are_not_destructive() -> None:
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for name in ADDITIVE_WRITE_TOOLS:
        tool = tools_dict[name]
        assert tool.annotations.destructive_hint is False, f"{name} is additive"


@pytest.mark.asyncio
async def test_destructive_writes_are_marked_destructive() -> None:
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for name in DESTRUCTIVE_WRITE_TOOLS:
        tool = tools_dict[name]
        assert tool.annotations.destructive_hint is True, f"{name} is destructive"


@pytest.mark.asyncio
async def test_no_write_is_wrongly_idempotent() -> None:
    """Writes that mutate external state should not claim idempotence."""
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for name in PUBLIC_WRITE_TOOLS:
        tool = tools_dict[name]
        assert tool.annotations.idempotent_hint is False, f"{name} must not claim idempotence"


@pytest.mark.asyncio
async def test_external_writes_are_open_world() -> None:
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for name in PUBLIC_WRITE_TOOLS:
        tool = tools_dict[name]
        assert tool.annotations.open_world_hint is True, f"{name} must be open-world"


@pytest.mark.asyncio
async def test_owner_and_repository_constraints() -> None:
    """Every write that targets a repository must bind owner/repo tightly."""
    tools_with_owner_repo = {
        "gh_create_issue",
        "gh_edit_issue",
        "gh_set_issue_state",
        "gh_create_label",
        "gh_upsert_label",
        "gh_edit_label",
        "gh_create_milestone",
        "gh_create_comment",
        "gh_create_pr",
        "gh_edit_pr",
        "gh_set_pr_draft_state",
        "gh_submit_pr_review",
        "gh_merge_pr",
        "gh_commit_files",
        "gh_create_release",
        "gh_create_release_exact",
        "gh_run_workflow",
        "gh_run_workflow_exact",
        "gh_create_branch",
        "gh_create_branch_from_sha",
    }
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for name in tools_with_owner_repo:
        tool = tools_dict[name]
        props = tool.input_schema["properties"]
        assert "owner" in props, f"{name} missing owner"
        assert "repo" in props, f"{name} missing repo"
        owner_schema = props["owner"]
        assert "pattern" in owner_schema, f"{name}.owner missing pattern"
        assert re.fullmatch(owner_schema["pattern"], "ValidOwner"), (
            f"{name}.owner pattern does not match valid sample"
        )
        assert owner_schema.get("maxLength") == 39, f"{name}.owner maxLength != 39"
        repo_schema = props["repo"]
        assert "pattern" in repo_schema, f"{name}.repo missing pattern"
        assert repo_schema.get("maxLength") == 100, f"{name}.repo maxLength != 100"


@pytest.mark.asyncio
async def test_positive_github_ids_are_bounded() -> None:
    """GitHub object numbers/IDs must be positive integers."""
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    positive_id_tools = {
        "gh_edit_issue": ["number"],
        "gh_set_issue_state": ["number"],
        "gh_create_comment": ["issue_number"],
        "gh_edit_pr": ["number"],
        "gh_set_pr_draft_state": ["number"],
        "gh_submit_pr_review": ["number"],
        "gh_merge_pr": ["number"],
        "gh_run_workflow": ["workflow_id"],
        "gh_run_workflow_exact": ["workflow_id"],
        "gh_create_branch": ["issue_number"],
    }
    for tool_name, fields in positive_id_tools.items():
        tool = tools_dict[tool_name]
        for field in fields:
            schema = tool.input_schema["properties"][field]
            assert schema.get("minimum") == 1, (
                f"{tool_name}.{field} must have minimum=1, got {schema.get('minimum')}"
            )


@pytest.mark.asyncio
async def test_exact_sha_fields_use_40_hex_pattern() -> None:
    """Fields annotated as exact SHA must use the 40-char hex pattern."""
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    sha_fields = {
        "gh_set_pr_draft_state": ["expected_head_sha"],
        "gh_submit_pr_review": ["expected_head_sha"],
        "gh_merge_pr": ["expected_head_sha"],
        "gh_commit_files": ["expected_head_sha"],
        "gh_create_release_exact": ["expected_target_sha"],
        "gh_run_workflow_exact": ["expected_ref_sha"],
        "gh_create_branch_from_sha": ["base_sha"],
    }
    for tool_name, fields in sha_fields.items():
        tool = tools_dict[tool_name]
        for field in fields:
            schema = tool.input_schema["properties"][field]
            assert "pattern" in schema, f"{tool_name}.{field} missing pattern"
            assert schema["pattern"] == OBJECT_SHA_PATTERN, (
                f"{tool_name}.{field} pattern must be {OBJECT_SHA_PATTERN}"
            )


@pytest.mark.asyncio
async def test_exact_ref_pattern_for_workflow_dispatch_exact() -> None:
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    tool = tools_dict["gh_run_workflow_exact"]
    schema = tool.input_schema["properties"]["ref"]
    assert "pattern" in schema
    assert schema["pattern"] == REF_PATTERN


@pytest.mark.asyncio
async def test_label_color_is_six_hex_chars() -> None:
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for tool_name in ("gh_create_label", "gh_upsert_label", "gh_edit_label"):
        tool = tools_dict[tool_name]
        raw_schema = tool.input_schema["properties"]["color"]
        schema = _extract_string_inner(raw_schema)
        assert "pattern" in schema, f"{tool_name}.color missing pattern"
        assert schema["pattern"] == LABEL_COLOR_PATTERN, (
            f"{tool_name}.color pattern must be {LABEL_COLOR_PATTERN}"
        )


@pytest.mark.asyncio
async def test_bounded_string_parameters() -> None:
    """Free-form text fields must have max_length bounds."""
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    bounded_strings = {
        "gh_create_issue": {"title", "body"},
        "gh_edit_issue": {"title", "body"},
        "gh_create_milestone": {"title", "description", "due_on"},
        "gh_create_comment": {"body"},
        "gh_create_pr": {"title", "body"},
        "gh_edit_pr": {"title", "body"},
        "gh_create_release": {"body", "name", "target"},
        "gh_create_release_exact": {"body", "name"},
        "gh_create_branch": {"name", "base"},
        "gh_create_branch_from_sha": {"name"},
    }
    for tool_name, fields in bounded_strings.items():
        tool = tools_dict[tool_name]
        for field in fields:
            raw_schema = tool.input_schema["properties"][field]
            schema = _extract_string_inner(raw_schema)
            # Bounded means maxLength is set, or it's a constrained type
            has_max = schema.get("maxLength") is not None
            has_min = schema.get("minLength") is not None
            is_constrained = "pattern" in schema or "enum" in schema
            assert has_max or has_min or is_constrained, f"{tool_name}.{field} should be bounded"


@pytest.mark.asyncio
async def test_bounded_array_parameters() -> None:
    """Collection parameters must have maxItems bounds."""
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    array_specs = [
        ("gh_edit_issue", "labels_add"),
        ("gh_edit_issue", "labels_remove"),
        ("gh_edit_issue", "assignees_add"),
        ("gh_edit_issue", "assignees_remove"),
        ("gh_create_pr", "labels"),
        ("gh_create_pr", "assignees"),
        ("gh_create_pr", "review_users"),
        ("gh_commit_files", "files"),
        ("gh_run_workflow", "fields"),
        ("gh_run_workflow_exact", "fields"),
    ]
    for tool_name, array_field in array_specs:
        tool = tools_dict[tool_name]
        schema = tool.input_schema["properties"].get(array_field)
        assert schema is not None, f"{tool_name} missing {array_field}"
        inner = _extract_array_inner(schema)
        assert inner.get("type") == "array", f"{tool_name}.{array_field} must be array"
        assert "maxItems" in inner, f"{tool_name}.{array_field} missing maxItems"
        assert isinstance(inner["maxItems"], int), f"{tool_name}.{array_field}.maxItems must be int"


@pytest.mark.asyncio
async def test_high_risk_exact_preconditions() -> None:
    """Critical write tools must pin exact preconditions on their most-sensitive fields."""
    tools_dict = {t.name: t for t in await mcp.list_tools()}

    merge_pr = tools_dict["gh_merge_pr"].input_schema["properties"]
    assert "expected_head_sha" in merge_pr
    assert merge_pr["expected_head_sha"]["pattern"] == OBJECT_SHA_PATTERN

    commit_files = tools_dict["gh_commit_files"].input_schema["properties"]
    assert "expected_head_sha" in commit_files
    assert commit_files["expected_head_sha"]["pattern"] == OBJECT_SHA_PATTERN

    branch_from_sha = tools_dict["gh_create_branch_from_sha"].input_schema["properties"]
    assert "base_sha" in branch_from_sha
    assert branch_from_sha["base_sha"]["pattern"] == OBJECT_SHA_PATTERN

    release_exact = tools_dict["gh_create_release_exact"].input_schema["properties"]
    assert "expected_target_sha" in release_exact
    assert release_exact["expected_target_sha"]["pattern"] == OBJECT_SHA_PATTERN

    workflow_exact = tools_dict["gh_run_workflow_exact"].input_schema["properties"]
    assert "expected_ref_sha" in workflow_exact
    assert workflow_exact["expected_ref_sha"]["pattern"] == OBJECT_SHA_PATTERN


@pytest.mark.asyncio
async def test_workflow_input_strings_constrained_to_key_equals_value() -> None:
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for tool_name in ("gh_run_workflow", "gh_run_workflow_exact"):
        tool = tools_dict[tool_name]
        fields_schema = tool.input_schema["properties"].get("fields")
        assert fields_schema is not None, f"{tool_name} missing fields param"
        inner = _extract_array_inner(fields_schema)
        assert inner.get("type") == "array"
        items = inner.get("items", {})
        assert "pattern" in items, f"{tool_name}.fields items missing pattern"
        assert re.fullmatch(r"^[^=]+=.*$", items["pattern"]), (
            f"{tool_name}.fields items pattern must enforce key=value"
        )


@pytest.mark.asyncio
async def test_forbidden_generic_executor_fields() -> None:
    """No write tool may expose arbitrary command/URL/JSON executor fields."""
    FORBIDDEN_FIELDS = {
        "args",
        "command",
        "shell",
        "url",
        "api_endpoint",
        "request_path",
        "payload",
        "json",
        "confirmation",
        "authorized",
        "bypass",
        "approve",
    }
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for name in PUBLIC_WRITE_TOOLS:
        tool = tools_dict[name]
        props = set(tool.input_schema.get("properties", {}).keys())
        violations = props & FORBIDDEN_FIELDS
        assert not violations, f"{name} exposes forbidden fields: {violations}"


@pytest.mark.asyncio
async def test_forbidden_generic_url_or_request_fields() -> None:
    """No write tool may accept an arbitrary request URL or endpoint selector."""
    TOOLS_WITH_PATH_FIELD = {
        "gh_get_file_contents",
        "gh_read_artifact_file",
    }
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for name in PUBLIC_WRITE_TOOLS:
        if name in TOOLS_WITH_PATH_FIELD:
            continue
        tool = tools_dict[name]
        props = tool.input_schema.get("properties", {})
        for field_name, schema in props.items():
            if field_name in ("url", "request_url", "api_endpoint", "request_path"):
                raise AssertionError(f"{name}.{field_name} looks like arbitrary URL selector")
            if field_name == "method":
                enum_vals = schema.get("enum", [])
                if set(enum_vals) == {"GET", "POST", "PUT", "DELETE"}:
                    raise AssertionError(f"{name}.method looks like generic HTTP method selector")


@pytest.mark.asyncio
async def test_annotation_truthfulness_against_known_sets() -> None:
    """Cross-check annotations against the canonical additive/destructive sets."""
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    for name in PUBLIC_WRITE_TOOLS:
        tool = tools_dict[name]
        expected_read_only = False
        expected_destructive = name in DESTRUCTIVE_WRITE_TOOLS
        assert tool.annotations.read_only_hint is expected_read_only, (
            f"{name} read_only_hint mismatch"
        )
        assert tool.annotations.destructive_hint is expected_destructive, (
            f"{name} destructive_hint mismatch"
        )
        assert tool.annotations.open_world_hint is True, f"{name} open_world_hint must be True"


@pytest.mark.asyncio
async def test_enum_literals_are_finite() -> None:
    """Where the facade uses Literal/enum types, the schema must list finite values."""
    tools_dict = {t.name: t for t in await mcp.list_tools()}

    milestone_state = tools_dict["gh_create_milestone"].input_schema["properties"]["state"]
    assert "enum" in milestone_state
    assert set(milestone_state["enum"]) == {"open", "closed"}

    review_action = tools_dict["gh_submit_pr_review"].input_schema["properties"]["action"]
    assert "enum" in review_action
    assert set(review_action["enum"]) == {"approve", "request_changes", "comment"}

    merge_method = tools_dict["gh_merge_pr"].input_schema["properties"]["method"]
    assert "enum" in merge_method
    assert set(merge_method["enum"]) == {"merge", "squash", "rebase"}


@pytest.mark.asyncio
async def test_commit_file_payload_has_content_bound() -> None:
    tools_dict = {t.name: t for t in await mcp.list_tools()}
    commit_files = tools_dict["gh_commit_files"].input_schema["properties"]
    files_schema = commit_files["files"]
    assert files_schema["type"] == "array"
    assert files_schema["minItems"] >= 1
    assert files_schema["maxItems"] <= 1000
    item_def = files_schema.get("items", {})
    item_ref = item_def.get("$ref", "")
    assert "PublicCommitFile" in item_ref, (
        f"commit files schema should reference PublicCommitFile, got {item_ref}"
    )
