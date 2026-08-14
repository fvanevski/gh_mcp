"""Policy regression for every public write-tool schema and host-facing metadata."""

from __future__ import annotations

import re
from collections.abc import Iterator

from mcp_gh_server.server import mcp
from mcp_gh_server.workflow_selector import WORKFLOW_PATH_RE
from mcp_gh_server.write_tool_schema import WRITE_TOOL_METADATA

# Independent expected surface. Do not derive this from WRITE_TOOL_METADATA: adding a
# public write must require an explicit policy-test update.
PUBLIC_WRITE_TOOLS = frozenset(
    {
        "gh_create_issue",
        "gh_edit_issue",
        "gh_set_issue_state",
        "gh_create_label",
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
        "gh_create_release_exact",
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
    "gh_create_release_exact",
    "gh_create_branch",
    "gh_create_branch_from_sha",
}

DESTRUCTIVE_WRITE_TOOLS = PUBLIC_WRITE_TOOLS - ADDITIVE_WRITE_TOOLS

OBJECT_SHA_PATTERN = r"^[0-9A-Fa-f]{40}$"
OWNER_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
ASSIGNEE_SELECTOR_PATTERN = r"^(?:@me|[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))$"
REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]{1,100}$"
REF_PATTERN = r"^(?:heads|tags)/.+$"
LABEL_COLOR_PATTERN = r"^[0-9A-Fa-f]{6}$"

FINE_GATE_DESCRIPTION_MARKERS = {
    "gh_merge_pr": "PR-merge fine gate",
    "gh_create_repo": "repository-creation fine gate",
    "gh_commit_files": "content-commit fine gate",
    "gh_create_release_exact": "release-creation fine gate",
    "gh_run_workflow_exact": "workflow-dispatch fine gate",
}

FORBIDDEN_FIELD_NAMES = {
    "admin",
    "approval",
    "args",
    "argv",
    "authorization",
    "authorized",
    "bypass",
    "command",
    "confirmation",
    "confirmed",
    "endpoint",
    "force",
    "json",
    "payload",
    "request_path",
    "request_url",
    "retry",
    "safety_justification",
    "shell",
    "url",
}

NON_CAPABILITY_MARKERS = (
    "cannot",
    "does not",
    "never",
    "no structured",
    "no unrelated",
    "rejected",
    "rejects",
    "separate",
    "unavailable",
)
PRECONDITION_MARKERS = (
    "authorization",
    "expected_",
    "exact",
    "gate",
    "policy",
    "precondition",
)


def _unwrap_optional(schema: dict) -> dict:
    """Return the one non-null branch of an optional JSON schema."""
    any_of = schema.get("anyOf")
    if not isinstance(any_of, list):
        return schema
    non_null = [item for item in any_of if item.get("type") != "null"]
    return non_null[0] if len(non_null) == 1 else schema


def _resolve_ref(root: dict, schema: dict) -> dict:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return schema
    name = ref.removeprefix("#/$defs/")
    resolved = root.get("$defs", {}).get(name)
    assert isinstance(resolved, dict), f"unresolved local schema ref: {ref}"
    return resolved


def _walk_schema(root: dict, schema: dict, path: str) -> Iterator[tuple[str, dict]]:
    """Walk local refs, unions, object properties, map keys/values, and array items."""
    schema = _resolve_ref(root, schema)
    yield path, schema

    for union_key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(union_key, [])
        if isinstance(variants, list):
            for index, variant in enumerate(variants):
                if isinstance(variant, dict) and variant.get("type") != "null":
                    yield from _walk_schema(root, variant, f"{path}.{union_key}[{index}]")

    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for name, child in properties.items():
            if isinstance(child, dict):
                yield from _walk_schema(root, child, f"{path}.{name}")

    property_names = schema.get("propertyNames")
    if isinstance(property_names, dict):
        yield from _walk_schema(root, property_names, f"{path}.<key>")

    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        yield from _walk_schema(root, additional, f"{path}.<value>")

    items = schema.get("items")
    if isinstance(items, dict):
        yield from _walk_schema(root, items, f"{path}[]")


async def _tools() -> dict:
    return {tool.name: tool for tool in await mcp.list_tools()}


async def test_exact_public_write_surface_is_independent_and_complete() -> None:
    tools = await _tools()
    actual_writes = {
        name
        for name, tool in tools.items()
        if tool.annotations is not None and tool.annotations.read_only_hint is False
    }
    assert actual_writes == PUBLIC_WRITE_TOOLS
    assert set(WRITE_TOOL_METADATA) == PUBLIC_WRITE_TOOLS
    assert len(PUBLIC_WRITE_TOOLS) == 18
    assert "gh_run_workflow" not in tools
    assert "gh_create_release" not in tools
    assert "gh_upsert_label" not in tools


async def test_registered_write_metadata_is_canonical_and_truthful() -> None:
    tools = await _tools()
    descriptions: set[str] = set()

    for name in PUBLIC_WRITE_TOOLS:
        tool = tools[name]
        metadata = WRITE_TOOL_METADATA[name]
        assert tool.title == metadata.title
        assert tool.description == metadata.description
        assert tool.annotations == metadata.annotations
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is (name in DESTRUCTIVE_WRITE_TOOLS)
        assert tool.annotations.idempotent_hint is False
        assert tool.annotations.open_world_hint is True

        description = tool.description or ""
        descriptions.add(description)
        assert description.startswith(("Additive write:", "Destructive write:"))
        assert any(marker in description for marker in PRECONDITION_MARKERS), name
        assert any(marker in description for marker in NON_CAPABILITY_MARKERS), name

    # Shared generic copy would defeat action-specific host review surfaces.
    assert len(descriptions) == len(PUBLIC_WRITE_TOOLS)


async def test_high_risk_write_descriptions_name_required_fine_gate() -> None:
    tools = await _tools()

    for tool_name, marker in FINE_GATE_DESCRIPTION_MARKERS.items():
        description = tools[tool_name].description or ""
        assert marker in description, f"{tool_name} must advertise {marker!r}"


async def test_repository_target_schemas_are_canonical() -> None:
    tools = await _tools()

    for name in PUBLIC_WRITE_TOOLS:
        properties = tools[name].input_schema["properties"]
        owner = properties["owner"]
        repo = properties["repo"]
        assert owner["pattern"] == OWNER_PATTERN
        assert owner["maxLength"] == 39
        assert repo["pattern"] == REPOSITORY_PATTERN
        assert repo["maxLength"] == 100

    create_properties = tools["gh_create_repo"].input_schema["properties"]
    assert "name" not in create_properties
    assert set(tools["gh_create_repo"].input_schema["required"]) >= {"owner", "repo"}


async def test_assignee_selectors_preserve_me_without_loosening_reviewers() -> None:
    tools = await _tools()
    assignee_fields = (
        ("gh_create_issue", "assignees"),
        ("gh_edit_issue", "assignees_add"),
        ("gh_edit_issue", "assignees_remove"),
        ("gh_create_pr", "assignees"),
        ("gh_edit_pr", "assignees_add"),
        ("gh_edit_pr", "assignees_remove"),
    )

    for tool_name, field_name in assignee_fields:
        raw = tools[tool_name].input_schema["properties"][field_name]
        assert "@me" in raw["description"]
        selectors = _unwrap_optional(raw)
        assert selectors["type"] == "array"
        assert selectors["maxItems"] == 10
        item = selectors["items"]
        assert item["pattern"] == ASSIGNEE_SELECTOR_PATTERN
        assert item["maxLength"] == 39
        assert re.fullmatch(item["pattern"], "octocat")
        assert re.fullmatch(item["pattern"], "@me")
        assert re.fullmatch(item["pattern"], "@you") is None
        assert re.fullmatch(item["pattern"], "octo/user") is None

    reviewers_raw = tools["gh_create_pr"].input_schema["properties"]["review_users"]
    reviewers = _unwrap_optional(reviewers_raw)
    assert reviewers["items"]["pattern"] == OWNER_PATTERN
    assert re.fullmatch(reviewers["items"]["pattern"], "@me") is None


async def test_all_write_schema_leaves_are_bounded() -> None:
    """Strings, integers, arrays, and map values must expose hard schema bounds."""
    tools = await _tools()

    for name in PUBLIC_WRITE_TOOLS:
        root = tools[name].input_schema
        for path, schema in _walk_schema(root, root, name):
            node_type = schema.get("type")
            if node_type == "string":
                assert (
                    "maxLength" in schema
                    or "pattern" in schema
                    or "enum" in schema
                    or "const" in schema
                ), f"{path} is an unbounded string"
            elif node_type == "integer":
                minimum = schema.get("minimum")
                assert isinstance(minimum, int) and minimum >= 1, (
                    f"{path} must be a positive bounded identifier"
                )
            elif node_type == "array":
                maximum = schema.get("maxItems")
                assert isinstance(maximum, int) and maximum >= 1, f"{path} is an unbounded array"
            elif node_type == "object":
                additional = schema.get("additionalProperties")
                assert additional not in (True, {}), f"{path} exposes arbitrary JSON"


async def test_no_generic_executor_or_host_bypass_surface_exists() -> None:
    tools = await _tools()

    for name in PUBLIC_WRITE_TOOLS:
        root = tools[name].input_schema
        for path, schema in _walk_schema(root, root, name):
            properties = schema.get("properties", {})
            if isinstance(properties, dict):
                violations = set(properties) & FORBIDDEN_FIELD_NAMES
                assert not violations, f"{path} exposes forbidden fields: {violations}"

                # `gh_merge_pr.method` is a finite merge strategy, not a generic HTTP
                # method. Only a generic HTTP verb surface is forbidden here.
                method = properties.get("method")
                if isinstance(method, dict):
                    method = _unwrap_optional(method)
                    verbs = set(method.get("enum", []))
                    assert verbs != {"GET", "POST", "PUT", "PATCH", "DELETE"}


async def test_exact_sha_ref_and_workflow_preconditions_are_host_visible() -> None:
    tools = await _tools()
    sha_fields = {
        "gh_set_pr_draft_state": "expected_head_sha",
        "gh_submit_pr_review": "expected_head_sha",
        "gh_merge_pr": "expected_head_sha",
        "gh_commit_files": "expected_head_sha",
        "gh_create_release_exact": "expected_target_sha",
        "gh_run_workflow_exact": "expected_ref_sha",
        "gh_create_branch_from_sha": "base_sha",
    }
    for tool_name, field in sha_fields.items():
        schema = tools[tool_name].input_schema["properties"][field]
        assert schema["pattern"] == OBJECT_SHA_PATTERN

    workflow = tools["gh_run_workflow_exact"].input_schema["properties"]
    assert workflow["workflow_id"]["type"] == "integer"
    assert workflow["workflow_id"]["minimum"] == 1
    assert workflow["expected_workflow_path"]["pattern"] == WORKFLOW_PATH_RE.pattern
    assert workflow["expected_workflow_path"]["maxLength"] == 1024
    assert workflow["ref"]["pattern"] == REF_PATTERN
    assert workflow["ref"]["maxLength"] == 1024


async def test_finite_enums_and_label_color_are_explicit() -> None:
    tools = await _tools()
    assert set(tools["gh_create_milestone"].input_schema["properties"]["state"]["enum"]) == {
        "open",
        "closed",
    }
    assert set(tools["gh_submit_pr_review"].input_schema["properties"]["action"]["enum"]) == {
        "approve",
        "request_changes",
        "comment",
    }
    assert set(tools["gh_merge_pr"].input_schema["properties"]["method"]["enum"]) == {
        "merge",
        "squash",
        "rebase",
    }

    for tool_name in ("gh_create_label", "gh_edit_label"):
        raw = tools[tool_name].input_schema["properties"]["color"]
        assert _unwrap_optional(raw)["pattern"] == LABEL_COLOR_PATTERN


async def test_workflow_inputs_are_a_bounded_typed_object() -> None:
    tools = await _tools()
    raw = tools["gh_run_workflow_exact"].input_schema["properties"]["inputs"]
    inputs = _unwrap_optional(raw)

    assert inputs["type"] == "object"
    assert inputs["maxProperties"] == 25
    assert inputs["propertyNames"]["minLength"] == 1
    assert inputs["propertyNames"]["maxLength"] == 65_535
    assert inputs["additionalProperties"]["type"] == "string"
    assert inputs["additionalProperties"]["maxLength"] == 65_535
    assert "65,535 aggregate" in raw["description"]


async def test_commit_file_payload_is_host_bounded() -> None:
    tools = await _tools()
    root = tools["gh_commit_files"].input_schema
    files = root["properties"]["files"]
    assert files["type"] == "array"
    assert files["minItems"] == 1
    assert files["maxItems"] == 1000

    item = _resolve_ref(root, files["items"])
    properties = item["properties"]
    assert properties["path"]["maxLength"] == 4096
    assert properties["content"]["maxLength"] == 5_000_000
    assert set(properties["mode"]["enum"]) == {"100644", "100755", "120000"}
