"""Regression coverage for ReviewerPrincipal / ReviewerIdentity resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

import mcp_gh_server.reviewer_auth as reviewer_auth_module
from mcp_gh_server.reviewer_auth import ReviewerIdentity, ReviewerPrincipal
from mcp_gh_server.settings import Settings

_VIEWER_QUERY = "query ReviewerIdentity { viewer { login } }"


@dataclass
class FakeGhClient:
    """Minimal GhClient stand-in for static-token reviewer tests."""

    viewer_login: str = "reviewer"
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        self.calls.append(args)
        assert args[0] == "api"
        assert args[1] == "graphql"
        return {"data": {"viewer": {"login": self.viewer_login}}}


def _static_settings(**overrides: Any) -> Settings:
    kwargs: dict[str, Any] = {"reviewer_token": "secret-token"}
    kwargs.update(overrides)
    return Settings(**kwargs)


def _app_settings(**overrides: Any) -> Settings:
    kwargs: dict[str, Any] = {
        "reviewer_app_id": 1,
        "reviewer_installation_id": 2,
        "reviewer_private_key_file": "/tmp/fake_key.pem",
        "reviewer_login": "app-bot[bot]",
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


# ---------------------------------------------------------------------------
# from_settings / kind
# ---------------------------------------------------------------------------


def test_from_settings_returns_none_when_not_configured() -> None:
    settings = Settings()
    assert not settings.reviewer_configured
    assert ReviewerPrincipal.from_settings(settings) is None


def test_from_settings_returns_static_principal_when_token_set() -> None:
    settings = _static_settings()
    principal = ReviewerPrincipal.from_settings(settings)
    assert principal is not None
    assert principal.kind == "static_token"


def test_from_settings_returns_app_principal_when_app_id_set() -> None:
    settings = _app_settings()
    principal = ReviewerPrincipal.from_settings(settings)
    assert principal is not None
    assert principal.kind == "github_app"


# ---------------------------------------------------------------------------
# resolve_identity_read_only — static path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_resolve_identity_matching_configured_login() -> None:
    settings = _static_settings(reviewer_login="reviewer")
    principal = ReviewerPrincipal.from_settings(settings)
    assert principal is not None
    fake = FakeGhClient(viewer_login="reviewer")
    with patch.object(reviewer_auth_module, "GhClient", return_value=fake):
        identity = await principal.resolve_identity_read_only("octo", "repo")
    assert identity == ReviewerIdentity(login="reviewer", kind="static_token")


@pytest.mark.asyncio
async def test_static_resolve_identity_case_insensitive_match() -> None:
    settings = _static_settings(reviewer_login="REVIEWER")
    principal = ReviewerPrincipal.from_settings(settings)
    assert principal is not None
    fake = FakeGhClient(viewer_login="reviewer")
    with patch.object(reviewer_auth_module, "GhClient", return_value=fake):
        identity = await principal.resolve_identity_read_only("octo", "repo")
    assert identity.login == "reviewer"


@pytest.mark.asyncio
async def test_static_resolve_identity_mismatch_raises() -> None:
    settings = _static_settings(reviewer_login="expected-login")
    principal = ReviewerPrincipal.from_settings(settings)
    assert principal is not None
    fake = FakeGhClient(viewer_login="actual-login")
    with (
        patch.object(reviewer_auth_module, "GhClient", return_value=fake),
        pytest.raises(RuntimeError, match="does not match the static reviewer token"),
    ):
        await principal.resolve_identity_read_only("octo", "repo")


@pytest.mark.asyncio
async def test_static_resolve_identity_no_configured_login_returns_viewer() -> None:
    settings = _static_settings()
    principal = ReviewerPrincipal.from_settings(settings)
    assert principal is not None
    fake = FakeGhClient(viewer_login="whoever")
    with patch.object(reviewer_auth_module, "GhClient", return_value=fake):
        identity = await principal.resolve_identity_read_only("octo", "repo")
    assert identity.login == "whoever"
    assert identity.kind == "static_token"


@pytest.mark.asyncio
async def test_static_resolve_identity_empty_viewer_raises() -> None:
    settings = _static_settings()
    principal = ReviewerPrincipal.from_settings(settings)
    assert principal is not None

    class BrokenClient:
        async def run(self, *args: str, **kwargs: Any) -> Any:
            return {"data": {"viewer": {}}}

    with (
        patch.object(reviewer_auth_module, "GhClient", return_value=BrokenClient()),
        pytest.raises(RuntimeError, match="did not return the authenticated"),
    ):
        await principal.resolve_identity_read_only("octo", "repo")


# ---------------------------------------------------------------------------
# client_for_review — static path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_client_for_review_matching_login_returns_client() -> None:
    settings = _static_settings()
    principal = ReviewerPrincipal.from_settings(settings)
    assert principal is not None
    fake = FakeGhClient(viewer_login="reviewer")
    with patch.object(reviewer_auth_module, "GhClient", return_value=fake):
        client = await principal.client_for_review("octo", "repo", expected_login="reviewer")
    assert client is fake


@pytest.mark.asyncio
async def test_static_client_for_review_mismatch_raises_value_error() -> None:
    settings = _static_settings()
    principal = ReviewerPrincipal.from_settings(settings)
    assert principal is not None
    fake = FakeGhClient(viewer_login="actual")
    with (
        patch.object(reviewer_auth_module, "GhClient", return_value=fake),
        pytest.raises(ValueError, match="does not match"),
    ):
        await principal.client_for_review("octo", "repo", expected_login="expected")


# ---------------------------------------------------------------------------
# Settings validation / fail-closed
# ---------------------------------------------------------------------------


def test_blank_reviewer_values_normalized_to_none() -> None:
    settings = Settings(
        reviewer_app_id="",
        reviewer_installation_id="",
        reviewer_private_key_file="",
        reviewer_login="",
        reviewer_token="",
    )
    assert settings.reviewer_app_id is None
    assert settings.reviewer_installation_id is None
    assert settings.reviewer_private_key_file is None
    assert settings.reviewer_login is None
    assert settings.reviewer_token is None
    assert not settings.reviewer_configured


def test_app_configured_requires_all_three_fields() -> None:
    with pytest.raises(ValueError, match="requires MCP_GH_REVIEWER_APP_ID"):
        Settings(reviewer_app_id=1)


def test_app_configured_requires_login() -> None:
    with pytest.raises(ValueError, match="requires MCP_GH_REVIEWER_LOGIN"):
        Settings(
            reviewer_app_id=1,
            reviewer_installation_id=2,
            reviewer_private_key_file="/tmp/key.pem",
        )


def test_app_and_token_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        Settings(
            reviewer_app_id=1,
            reviewer_installation_id=2,
            reviewer_private_key_file="/tmp/key.pem",
            reviewer_login="bot[bot]",
            reviewer_token="some-token",
        )


def test_reviewer_login_without_credentials_fails() -> None:
    with pytest.raises(ValueError, match="requires reviewer credentials"):
        Settings(reviewer_login="someone")


def test_reviewer_configured_is_true_only_for_token_or_app() -> None:
    assert not Settings().reviewer_configured
    assert Settings(reviewer_token="x").reviewer_configured
    assert Settings(
        reviewer_app_id=1,
        reviewer_installation_id=2,
        reviewer_private_key_file="/tmp/k.pem",
        reviewer_login="bot[bot]",
    ).reviewer_configured


# ---------------------------------------------------------------------------
# ReviewerIdentity is a frozen dataclass
# ---------------------------------------------------------------------------


def test_reviewer_identity_is_frozen() -> None:
    identity = ReviewerIdentity(login="x", kind="static_token")
    with pytest.raises(AttributeError):
        identity.login = "y"  # type: ignore[misc]
