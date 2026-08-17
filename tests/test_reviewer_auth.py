"""Regression coverage for reviewer-only authentication and principal isolation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.error import URLError
from urllib.request import Request
from unittest.mock import patch

import pytest

import mcp_gh_server.reviewer_auth as reviewer_auth_module
from mcp_gh_server.request_governor import GitHubRequestError
from mcp_gh_server.reviewer_auth import ReviewerIdentity, ReviewerPrincipal
from mcp_gh_server.settings import Settings

_VIEWER_QUERY = "query ReviewerIdentity { viewer { login } }"


@dataclass
class FakeGhClient:
    """Minimal GhClient stand-in that exposes only authenticated viewer identity."""

    viewer_login: str = "reviewer"
    settings: Settings | None = None
    governor: Any = None
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def run(self, *args: str, **kwargs: Any) -> Any:
        del kwargs
        self.calls.append(args)
        assert args == ("api", "graphql", "-f", f"query={_VIEWER_QUERY}")
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


def _principal(settings: Settings) -> ReviewerPrincipal:
    principal = ReviewerPrincipal.from_settings(settings)
    assert principal is not None
    return principal


def test_from_settings_returns_none_when_not_configured() -> None:
    settings = Settings()
    assert not settings.reviewer_configured
    assert ReviewerPrincipal.from_settings(settings) is None


def test_from_settings_returns_static_principal_when_token_set() -> None:
    assert _principal(_static_settings()).kind == "static_token"


def test_from_settings_returns_app_principal_when_app_id_set() -> None:
    assert _principal(_app_settings()).kind == "github_app"


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
    assert _app_settings().reviewer_configured


@pytest.mark.asyncio
async def test_static_resolve_identity_matching_configured_login() -> None:
    principal = _principal(_static_settings(reviewer_login="reviewer"))
    fake = FakeGhClient(viewer_login="reviewer")
    with patch.object(reviewer_auth_module, "GhClient", return_value=fake):
        identity = await principal.resolve_identity_read_only("octo", "repo")
    assert identity == ReviewerIdentity(login="reviewer", kind="static_token")


@pytest.mark.asyncio
async def test_static_resolve_identity_case_insensitive_match() -> None:
    principal = _principal(_static_settings(reviewer_login="REVIEWER"))
    fake = FakeGhClient(viewer_login="reviewer")
    with patch.object(reviewer_auth_module, "GhClient", return_value=fake):
        identity = await principal.resolve_identity_read_only("octo", "repo")
    assert identity.login == "reviewer"


@pytest.mark.asyncio
async def test_static_resolve_identity_mismatch_raises() -> None:
    principal = _principal(_static_settings(reviewer_login="expected-login"))
    fake = FakeGhClient(viewer_login="actual-login")
    with (
        patch.object(reviewer_auth_module, "GhClient", return_value=fake),
        pytest.raises(RuntimeError, match="does not match the static reviewer token"),
    ):
        await principal.resolve_identity_read_only("octo", "repo")


@pytest.mark.asyncio
async def test_static_resolve_identity_no_configured_login_returns_viewer() -> None:
    principal = _principal(_static_settings())
    fake = FakeGhClient(viewer_login="whoever")
    with patch.object(reviewer_auth_module, "GhClient", return_value=fake):
        identity = await principal.resolve_identity_read_only("octo", "repo")
    assert identity == ReviewerIdentity(login="whoever", kind="static_token")


@pytest.mark.asyncio
async def test_static_resolve_identity_empty_viewer_raises() -> None:
    principal = _principal(_static_settings())

    class BrokenClient:
        async def run(self, *args: str, **kwargs: Any) -> Any:
            del args, kwargs
            return {"data": {"viewer": {}}}

    with (
        patch.object(reviewer_auth_module, "GhClient", return_value=BrokenClient()),
        pytest.raises(RuntimeError, match="did not return the authenticated"),
    ):
        await principal.resolve_identity_read_only("octo", "repo")


@pytest.mark.asyncio
async def test_static_client_for_review_uses_reviewer_token_without_mutating_ordinary_settings() -> None:
    settings = _static_settings(github_token="ordinary-token")
    principal = _principal(settings)
    created: list[FakeGhClient] = []

    def make_client(*, settings: Settings, governor: Any) -> FakeGhClient:
        client = FakeGhClient(viewer_login="reviewer", settings=settings, governor=governor)
        created.append(client)
        return client

    with patch.object(reviewer_auth_module, "GhClient", side_effect=make_client):
        client = await principal.client_for_review("octo", "repo", expected_login="reviewer")

    assert client is created[0]
    assert created[0].settings is not None
    assert created[0].settings.github_token is not None
    assert created[0].settings.github_token.get_secret_value() == "secret-token"
    assert settings.github_token is not None
    assert settings.github_token.get_secret_value() == "ordinary-token"


@pytest.mark.asyncio
async def test_static_client_for_review_mismatch_raises_value_error() -> None:
    principal = _principal(_static_settings())
    fake = FakeGhClient(viewer_login="actual")
    with (
        patch.object(reviewer_auth_module, "GhClient", return_value=fake),
        pytest.raises(ValueError, match="does not match"),
    ):
        await principal.client_for_review("octo", "repo", expected_login="expected")


@pytest.mark.asyncio
async def test_app_read_only_resolves_configured_identity_without_minting_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal(_app_settings())
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = []

    async def fake_jwt(self: ReviewerPrincipal, app_id: int) -> str:
        del self
        assert app_id == 1
        return "app-jwt"

    async def fake_request(
        self: ReviewerPrincipal,
        method: str,
        endpoint: str,
        token: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del self
        calls.append((method, endpoint, token, payload))
        if endpoint == "app":
            return {"id": 1}
        assert endpoint == "repos/octo/repo/installation"
        return {"id": 2, "permissions": {"pull_requests": "write"}, "suspended_at": None}

    monkeypatch.setattr(ReviewerPrincipal, "_app_jwt", fake_jwt)
    monkeypatch.setattr(ReviewerPrincipal, "_app_request", fake_request)

    identity = await principal.resolve_identity_read_only("octo", "repo")

    assert identity == ReviewerIdentity(login="app-bot[bot]", kind="github_app")
    assert calls == [
        ("GET", "app", "app-jwt", None),
        ("GET", "repos/octo/repo/installation", "app-jwt", None),
    ]
    assert all(method == "GET" for method, _, _, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("installation", "message"),
    [
        (
            {"id": 999, "permissions": {"pull_requests": "write"}, "suspended_at": None},
            "not the installation for the target repository",
        ),
        (
            {"id": 2, "permissions": {"pull_requests": "read"}, "suspended_at": None},
            "lacks pull_requests=write",
        ),
        (
            {
                "id": 2,
                "permissions": {"pull_requests": "write"},
                "suspended_at": "2026-08-17T00:00:00Z",
            },
            "installation is suspended",
        ),
    ],
)
async def test_app_read_only_identity_fails_closed_on_invalid_installation(
    monkeypatch: pytest.MonkeyPatch,
    installation: dict[str, Any],
    message: str,
) -> None:
    principal = _principal(_app_settings())
    methods: list[str] = []

    async def fake_jwt(self: ReviewerPrincipal, app_id: int) -> str:
        del self, app_id
        return "app-jwt"

    async def fake_request(
        self: ReviewerPrincipal,
        method: str,
        endpoint: str,
        token: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del self, token, payload
        methods.append(method)
        return {"id": 1} if endpoint == "app" else installation

    monkeypatch.setattr(ReviewerPrincipal, "_app_jwt", fake_jwt)
    monkeypatch.setattr(ReviewerPrincipal, "_app_request", fake_request)

    with pytest.raises(RuntimeError, match=message):
        await principal.resolve_identity_read_only("octo", "repo")

    assert methods == ["GET", "GET"]


@pytest.mark.asyncio
async def test_app_client_for_review_mints_one_repo_scoped_token_and_verifies_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _app_settings(github_token="ordinary-token")
    principal = _principal(settings)
    app_calls: list[tuple[str, str, dict[str, Any] | None]] = []
    created_clients: list[FakeGhClient] = []

    async def fake_jwt(self: ReviewerPrincipal, app_id: int) -> str:
        del self
        assert app_id == 1
        return "app-jwt"

    async def fake_request(
        self: ReviewerPrincipal,
        method: str,
        endpoint: str,
        token: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del self
        assert token == "app-jwt"
        app_calls.append((method, endpoint, payload))
        if method == "GET":
            assert endpoint == "repos/octo/repo/installation"
            return {"id": 2, "permissions": {"pull_requests": "write"}, "suspended_at": None}
        assert endpoint == "app/installations/2/access_tokens"
        return {"token": "installation-token"}

    def make_client(*, settings: Settings, governor: Any) -> FakeGhClient:
        client = FakeGhClient(
            viewer_login="app-bot[bot]", settings=settings, governor=governor
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(ReviewerPrincipal, "_app_jwt", fake_jwt)
    monkeypatch.setattr(ReviewerPrincipal, "_app_request", fake_request)
    monkeypatch.setattr(reviewer_auth_module, "GhClient", make_client)

    client = await principal.client_for_review(
        "octo", "repo", expected_login="app-bot[bot]"
    )

    assert client is created_clients[0]
    assert app_calls == [
        ("GET", "repos/octo/repo/installation", None),
        (
            "POST",
            "app/installations/2/access_tokens",
            {"repositories": ["repo"], "permissions": {"pull_requests": "write"}},
        ),
    ]
    assert created_clients[0].settings is not None
    assert created_clients[0].settings.github_token is not None
    assert created_clients[0].settings.github_token.get_secret_value() == "installation-token"
    assert created_clients[0].governor is principal.governor
    assert settings.github_token is not None
    assert settings.github_token.get_secret_value() == "ordinary-token"
    assert created_clients[0].calls == [("api", "graphql", "-f", f"query={_VIEWER_QUERY}")]


@pytest.mark.asyncio
async def test_app_client_for_review_rejects_wrong_authenticated_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal(_app_settings())
    token_mints = 0

    async def fake_jwt(self: ReviewerPrincipal, app_id: int) -> str:
        del self, app_id
        return "app-jwt"

    async def fake_request(
        self: ReviewerPrincipal,
        method: str,
        endpoint: str,
        token: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        nonlocal token_mints
        del self, token, payload
        if method == "GET":
            return {"id": 2, "permissions": {"pull_requests": "write"}, "suspended_at": None}
        assert endpoint == "app/installations/2/access_tokens"
        token_mints += 1
        return {"token": "installation-token"}

    monkeypatch.setattr(ReviewerPrincipal, "_app_jwt", fake_jwt)
    monkeypatch.setattr(ReviewerPrincipal, "_app_request", fake_request)
    monkeypatch.setattr(
        reviewer_auth_module,
        "GhClient",
        lambda **kwargs: FakeGhClient(
            viewer_login="different-bot[bot]",
            settings=kwargs["settings"],
            governor=kwargs["governor"],
        ),
    )

    with pytest.raises(ValueError, match="does not match expected_reviewer_login"):
        await principal.client_for_review(
            "octo", "repo", expected_login="app-bot[bot]"
        )

    assert token_mints == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("installation", "message"),
    [
        (
            {"id": 999, "permissions": {"pull_requests": "write"}, "suspended_at": None},
            "no longer matches the target repository",
        ),
        (
            {"id": 2, "permissions": {"pull_requests": "read"}, "suspended_at": None},
            "no longer has pull_requests=write",
        ),
        (
            {
                "id": 2,
                "permissions": {"pull_requests": "write"},
                "suspended_at": "2026-08-17T00:00:00Z",
            },
            "installation is suspended",
        ),
    ],
)
async def test_app_client_for_review_fails_before_token_mint_on_invalid_installation(
    monkeypatch: pytest.MonkeyPatch,
    installation: dict[str, Any],
    message: str,
) -> None:
    principal = _principal(_app_settings())
    methods: list[str] = []

    async def fake_jwt(self: ReviewerPrincipal, app_id: int) -> str:
        del self, app_id
        return "app-jwt"

    async def fake_request(
        self: ReviewerPrincipal,
        method: str,
        endpoint: str,
        token: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del self, endpoint, token, payload
        methods.append(method)
        return installation

    monkeypatch.setattr(ReviewerPrincipal, "_app_jwt", fake_jwt)
    monkeypatch.setattr(ReviewerPrincipal, "_app_request", fake_request)

    with pytest.raises(RuntimeError, match=message):
        await principal.client_for_review(
            "octo", "repo", expected_login="app-bot[bot]"
        )

    assert methods == ["GET"]


@pytest.mark.asyncio
async def test_app_jwt_uses_openssl_without_shell_and_does_not_reflect_key_material(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    key_path = tmp_path / "reviewer.pem"
    key_material = "-----BEGIN PRIVATE KEY-----\nTOP-SECRET\n-----END PRIVATE KEY-----\n"
    key_path.write_text(key_material)
    principal = _principal(_app_settings(reviewer_private_key_file=key_path))
    invocation: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, signing_input: bytes) -> tuple[bytes, bytes]:
            invocation["signing_input"] = signing_input
            return b"signature-bytes", b""

        def kill(self) -> None:
            raise AssertionError("successful signing must not kill the process")

        async def wait(self) -> None:
            raise AssertionError("successful signing must not wait after kill")

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> FakeProcess:
        invocation["args"] = args
        invocation["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(reviewer_auth_module, "time", lambda: 1_700_000_000)

    token = await principal._app_jwt(1)

    assert invocation["args"] == (
        "openssl",
        "dgst",
        "-sha256",
        "-sign",
        str(key_path),
    )
    assert invocation["kwargs"] == {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    assert key_material.encode() not in invocation["signing_input"]
    assert token.count(".") == 2


@pytest.mark.asyncio
async def test_app_jwt_missing_key_does_not_disclose_local_path(tmp_path: Any) -> None:
    missing = tmp_path / "secret" / "reviewer.pem"
    principal = _principal(_app_settings(reviewer_private_key_file=missing))

    with pytest.raises(RuntimeError) as caught:
        await principal._app_jwt(1)

    assert "does not exist" in str(caught.value)
    assert str(missing) not in str(caught.value)


@pytest.mark.asyncio
async def test_app_jwt_signing_failure_does_not_reflect_openssl_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    key_path = tmp_path / "reviewer.pem"
    key_path.write_text("dummy")
    principal = _principal(_app_settings(reviewer_private_key_file=key_path))
    sensitive_detail = f"unable to read {key_path}"

    class FakeProcess:
        returncode = 1

        async def communicate(self, signing_input: bytes) -> tuple[bytes, bytes]:
            del signing_input
            return b"", sensitive_detail.encode()

        def kill(self) -> None:
            raise AssertionError("non-timeout signing failure must not kill")

        async def wait(self) -> None:
            raise AssertionError("non-timeout signing failure must not wait")

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> FakeProcess:
        del args, kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError) as caught:
        await principal._app_jwt(1)

    assert str(caught.value) == "Unable to sign reviewer GitHub App JWT"
    assert sensitive_detail not in str(caught.value)


class _FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = json.dumps(body).encode()
        self.headers = {
            "X-GitHub-Request-Id": "request-id",
            "X-RateLimit-Remaining": "99",
            "X-RateLimit-Reset": "1700000100",
        }

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        del args

    def read(self, limit: int) -> bytes:
        assert limit == reviewer_auth_module._MAX_APP_RESPONSE_BYTES + 1
        return self._body


def test_app_request_once_uses_fixed_github_api_bearer_and_exact_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    payload = {"repositories": ["repo"], "permissions": {"pull_requests": "write"}}

    def fake_urlopen(request: Request, *, timeout: int) -> _FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse({"token": "installation-token"})

    monkeypatch.setattr(reviewer_auth_module, "urlopen", fake_urlopen)

    result = reviewer_auth_module._app_request_once(
        "POST", "app/installations/2/access_tokens", "app-jwt", payload
    )

    request = captured["request"]
    assert request.full_url == "https://api.github.com/app/installations/2/access_tokens"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer app-jwt"
    assert request.get_header("X-github-api-version") == "2022-11-28"
    assert json.loads(request.data) == payload
    assert captured["timeout"] == 30
    assert result.value == {"token": "installation-token"}
    assert result.metadata.request_id == "request-id"


@pytest.mark.parametrize(("method", "ambiguous"), [("GET", False), ("POST", True)])
def test_app_request_transport_failure_marks_only_post_as_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    ambiguous: bool,
) -> None:
    def fail_urlopen(request: Request, *, timeout: int) -> _FakeResponse:
        del request, timeout
        raise URLError("transport reset")

    monkeypatch.setattr(reviewer_auth_module, "urlopen", fail_urlopen)

    with pytest.raises(GitHubRequestError) as caught:
        reviewer_auth_module._app_request_once(
            method,  # type: ignore[arg-type]
            "app",
            "app-jwt",
            None,
        )

    assert caught.value.retryable is True
    assert caught.value.ambiguous is ambiguous


def test_reviewer_identity_is_frozen() -> None:
    identity = ReviewerIdentity(login="x", kind="static_token")
    with pytest.raises(AttributeError):
        identity.login = "y"  # type: ignore[misc]
