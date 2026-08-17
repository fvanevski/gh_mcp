"""Reviewer-only GitHub authentication for formal pull-request reviews."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import SecretStr

from .gh_client import GhClient
from .request_governor import (
    READ_REQUEST,
    WRITE_REQUEST,
    GitHubRequestError,
    GitHubRequestGovernor,
    GitHubRequestMetadata,
    GitHubRequestResult,
)
from .settings import Settings

_API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_MAX_APP_RESPONSE_BYTES = 1_000_000
_VIEWER_QUERY = "query ReviewerIdentity { viewer { login } }"


@dataclass(frozen=True, slots=True)
class ReviewerIdentity:
    """Resolved reviewer principal evidence without exposing credentials."""

    login: str
    kind: Literal["github_app", "static_token"]


@dataclass(slots=True)
class ReviewerPrincipal:
    """A reviewer-only principal that never reuses the ordinary GitHub credential."""

    settings: Settings
    governor: GitHubRequestGovernor

    @classmethod
    def from_settings(cls, settings: Settings) -> ReviewerPrincipal | None:
        if not settings.reviewer_configured:
            return None
        return cls(settings=settings, governor=GitHubRequestGovernor())

    @property
    def kind(self) -> Literal["github_app", "static_token"]:
        return "github_app" if self.settings.reviewer_app_id is not None else "static_token"

    async def resolve_identity_read_only(
        self,
        owner: str,
        repo: str,
    ) -> ReviewerIdentity:
        """Resolve reviewer identity without minting an installation token."""

        if self.kind == "static_token":
            client = self._static_client()
            login = await _viewer_login(client)
            configured = self.settings.reviewer_login
            if configured is not None and login.casefold() != configured.casefold():
                raise RuntimeError(
                    "Configured reviewer login does not match the static reviewer token"
                )
            return ReviewerIdentity(login=login, kind="static_token")

        app_id = _required_int(self.settings.reviewer_app_id, "reviewer_app_id")
        installation_id = _required_int(
            self.settings.reviewer_installation_id,
            "reviewer_installation_id",
        )
        configured_login = self.settings.reviewer_login
        if configured_login is None:
            raise RuntimeError(
                "GitHub App reviewer configuration requires MCP_GH_REVIEWER_LOGIN "
                "for read-only eligibility; write-side identity is independently verified"
            )

        token = await self._app_jwt(app_id)
        app = await self._app_request("GET", "app", token)
        returned_app_id = app.get("id")
        if returned_app_id != app_id:
            raise RuntimeError("GitHub authenticated App identity does not match reviewer_app_id")

        installation = await self._app_request(
            "GET",
            f"repos/{owner}/{repo}/installation",
            token,
        )
        returned_installation_id = installation.get("id")
        if returned_installation_id != installation_id:
            raise RuntimeError(
                "Configured reviewer installation is not the installation for the target repository"
            )
        permissions = installation.get("permissions")
        pull_requests = permissions.get("pull_requests") if isinstance(permissions, dict) else None
        if pull_requests != "write":
            raise RuntimeError(
                "Configured reviewer installation lacks pull_requests=write permission"
            )
        if installation.get("suspended_at") is not None:
            raise RuntimeError("Configured reviewer installation is suspended")

        return ReviewerIdentity(login=configured_login, kind="github_app")

    async def client_for_review(
        self,
        owner: str,
        repo: str,
        *,
        expected_login: str,
    ) -> GhClient:
        """Return an independently authenticated reviewer client proven to expected_login."""

        if self.kind == "static_token":
            client = self._static_client()
            actual = await _viewer_login(client)
            if actual.casefold() != expected_login.casefold():
                raise ValueError(
                    f"authenticated reviewer login {actual!r} does not match "
                    f"expected_reviewer_login {expected_login!r}; no review was attempted"
                )
            return client

        app_id = _required_int(self.settings.reviewer_app_id, "reviewer_app_id")
        installation_id = _required_int(
            self.settings.reviewer_installation_id,
            "reviewer_installation_id",
        )
        token = await self._app_jwt(app_id)

        installation = await self._app_request(
            "GET",
            f"repos/{owner}/{repo}/installation",
            token,
        )
        if installation.get("id") != installation_id:
            raise RuntimeError(
                "Configured reviewer installation no longer matches the target repository"
            )
        permissions = installation.get("permissions")
        pull_requests = permissions.get("pull_requests") if isinstance(permissions, dict) else None
        if pull_requests != "write":
            raise RuntimeError(
                "Configured reviewer installation no longer has pull_requests=write permission"
            )
        if installation.get("suspended_at") is not None:
            raise RuntimeError("Configured reviewer installation is suspended")

        created = await self._app_request(
            "POST",
            f"app/installations/{installation_id}/access_tokens",
            token,
            payload={
                "repositories": [repo],
                "permissions": {"pull_requests": "write"},
            },
        )
        installation_token = created.get("token")
        if not isinstance(installation_token, str) or not installation_token:
            raise RuntimeError("GitHub did not return an installation access token")

        reviewer_settings = self.settings.model_copy(
            update={"github_token": SecretStr(installation_token)}
        )
        client = GhClient(settings=reviewer_settings, governor=self.governor)
        actual = await _viewer_login(client)
        if actual.casefold() != expected_login.casefold():
            raise ValueError(
                f"authenticated reviewer login {actual!r} does not match "
                f"expected_reviewer_login {expected_login!r}; no review was attempted"
            )
        return client

    def _static_client(self) -> GhClient:
        token = self.settings.reviewer_token
        if token is None:
            raise RuntimeError("Static reviewer token is not configured")
        reviewer_settings = self.settings.model_copy(update={"github_token": token})
        return GhClient(settings=reviewer_settings, governor=self.governor)

    async def _app_jwt(self, app_id: int) -> str:
        key_file = self.settings.reviewer_private_key_file
        if key_file is None:
            raise RuntimeError("GitHub App reviewer private key file is not configured")
        if not Path(key_file).is_file():
            raise RuntimeError(f"Reviewer GitHub App private key file does not exist: {key_file}")

        now = int(time())
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
        signing_input = b".".join(
            (
                _base64url_json(header),
                _base64url_json(payload),
            )
        )

        try:
            process = await asyncio.create_subprocess_exec(
                "openssl",
                "dgst",
                "-sha256",
                "-sign",
                str(key_file),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RuntimeError(
                "Reviewer GitHub App authentication requires the openssl executable"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(signing_input),
                timeout=float(self.settings.command_timeout_seconds),
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError("Reviewer GitHub App JWT signing timed out") from exc

        if process.returncode != 0 or not stdout:
            detail = (stderr or b"").decode(errors="replace").strip()[:1000]
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Unable to sign reviewer GitHub App JWT{suffix}")

        return (
            signing_input.decode("ascii")
            + "."
            + base64.urlsafe_b64encode(stdout).rstrip(b"=").decode("ascii")
        )

    async def _app_request(
        self,
        method: Literal["GET", "POST"],
        endpoint: str,
        jwt_token: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        policy = READ_REQUEST if method == "GET" else WRITE_REQUEST

        async def operation() -> GitHubRequestResult[dict[str, Any]]:
            return await asyncio.to_thread(
                _app_request_once,
                method,
                endpoint,
                jwt_token,
                payload,
            )

        result = await self.governor.execute(policy, operation)
        return result.value


def _base64url_json(value: dict[str, Any]) -> bytes:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


async def _viewer_login(client: GhClient) -> str:
    result = await client.run(
        "api",
        "graphql",
        "-f",
        f"query={_VIEWER_QUERY}",
    )
    data = result.get("data") if isinstance(result, dict) else None
    viewer = data.get("viewer") if isinstance(data, dict) else None
    login = viewer.get("login") if isinstance(viewer, dict) else None
    if not isinstance(login, str) or not login:
        raise RuntimeError("GitHub did not return the authenticated reviewer login")
    return login


def _required_int(value: int | None, name: str) -> int:
    if value is None:
        raise RuntimeError(f"{name} is not configured")
    return value


def _app_request_once(
    method: Literal["GET", "POST"],
    endpoint: str,
    jwt_token: str,
    payload: dict[str, Any] | None,
) -> GitHubRequestResult[dict[str, Any]]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = Request(
        f"{_API_ROOT}/{endpoint.lstrip('/')}",
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
            "User-Agent": "mcp-gh-server-reviewer",
            "X-GitHub-Api-Version": _API_VERSION,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(_MAX_APP_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_APP_RESPONSE_BYTES:
                raise GitHubRequestError(
                    "GitHub App API response exceeded the bounded response limit"
                )
            metadata = _response_metadata(response.headers)
    except HTTPError as exc:
        detail = _safe_error_detail(exc)
        raise GitHubRequestError(
            f"GitHub App API {method} {endpoint} failed with HTTP {exc.code}{detail}",
            status_code=exc.code,
            retryable=exc.code in {408, 500, 502, 503, 504},
            ambiguous=False,
            metadata=_response_metadata(exc.headers),
        ) from exc
    except URLError as exc:
        raise GitHubRequestError(
            f"GitHub App API {method} {endpoint} transport failure",
            retryable=True,
            ambiguous=method == "POST",
        ) from exc

    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubRequestError("GitHub App API returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise GitHubRequestError("GitHub App API returned a non-object JSON response")
    return GitHubRequestResult(value=parsed, metadata=metadata)


def _response_metadata(headers: Any) -> GitHubRequestMetadata:
    def as_int(name: str) -> int | None:
        value = headers.get(name) if headers is not None else None
        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    return GitHubRequestMetadata(
        request_id=headers.get("X-GitHub-Request-Id") if headers is not None else None,
        rate_limit_reset_epoch=as_int("X-RateLimit-Reset"),
        rate_limit_remaining=as_int("X-RateLimit-Remaining"),
    )


def _safe_error_detail(error: HTTPError) -> str:
    try:
        raw = error.read(16_384)
        parsed = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        del exc
        return ""
    if not isinstance(parsed, dict):
        return ""
    message = parsed.get("message")
    return f": {message[:1000]}" if isinstance(message, str) and message else ""
